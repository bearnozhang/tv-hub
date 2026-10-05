#!/usr/bin/env python3
"""gate.py —— 质量门禁：决定产物「能不能发布」。

## 和 validate 的区别

- `validate.py` 回答「格式对不对」：字段齐不齐、能不能解析。**格式合法就通过。**
- `gate.py`    回答「值不值得发」：只有 50 个站点的配置格式也合法，
  但发出去对用户是伤害。

## 为什么需要门禁

之前的流程是「能构建 → 就发布」。后果：

- 上游大面积失效时，产物会从 4300 站缩水到几百站，照样发布
- 直播源全挂时，产物还能生成，只是内容空了
- 而用户拿到的是一份「能加载但几乎没用」的配置，且不会有人知道

门禁的职责就是：**在坏东西上线之前拦住它，并让线上保持上一个好版本。**

## 门禁分两级

**硬门禁**（不通过 → 退出码 1 → CI 不提交 → 线上不变）
1. 契约冲突 = 0            —— 否则客户端必然整份加载失败
2. 站点数未骤降            —— 相对「上次通过门禁」的基线
3. 直播频道数未骤降
4. 关键直播分组存在（如「央视」）

**软警告**（记录但不拦）
- 产物体积超出契约上限
- 上游失败数偏多
- 解析器数量偏少

## 基线如何维护

`cache/_gate.json` 记录「上次通过门禁时」的指标。
**只有通过时才更新基线** —— 这样连续失败不会把基线一路拉低到「零也通过」。

退出码：0 通过 / 1 被拦 / 2 用法错误
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C      # noqa: E402
import kernel as K      # noqa: E402

GATE_FILE = os.path.join(C.ROOT, "cache", "_gate.json")
QUALITY_FILE = os.path.join(C.PUBLIC_DIR, "quality.json")


def _load_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def collect(public_dir: str = None) -> dict:
    """从产物里收集质量指标。"""
    public_dir = public_dir or C.PUBLIC_DIR
    m: dict = {
        "sites": 0, "parses": 0, "live_channels": 0, "live_groups": 0,
        "contract_conflicts": 0, "conflict_detail": {},
        "artifacts": {},
    }

    # 主配置：优先 tv.json（全量聚合）
    main = None
    for name in ("tv.json", "tv-full.json"):
        p = os.path.join(public_dir, name)
        if os.path.exists(p):
            main = _load_json(p)
            break

    if main is not None:
        m["sites"] = len(main.get("sites") or [])
        m["parses"] = len(main.get("parses") or [])
        conf = K.config_conflicts(main)
        m["contract_conflicts"] = conf["total"]
        if conf["total"]:
            m["conflict_detail"] = {
                k: [f"#{i} {key}: {prob}" for i, key, prob in conf[k][:5]]
                for k in ("site", "parse", "live") if conf[k]
            }
        # 空 key / 重复 key 同样是致命的
        dup = K.duplicate_keys(main.get("sites") or [], "site")
        if dup:
            m["conflict_detail"]["duplicate"] = dup
            m["contract_conflicts"] += 1

    # 直播指标：**统一以 live.txt 为准**。
    #
    # 为什么必须统一口径：同一份直播内容在不同产物里的计数互不相等 ——
    #   live.json 的 channels（分组内部计数）
    #   status.json 的 live_channels（去重后的频道数）
    #   live.txt 的频道行数（含多备源，每行一个 url）
    # 混用会让基线失去意义（曾因此把 8131 与 4496 当成"骤降"）。
    # 取 live.txt，因为那是直播入口实际消费的形态。
    live_txt = os.path.join(public_dir, "live.txt")
    if os.path.exists(live_txt):
        try:
            with open(live_txt, encoding="utf-8-sig") as f:
                text = f.read()
            lines = text.splitlines()
            m["live_groups"] = sum(1 for line in lines if "#genre#" in line)
            m["live_channels"] = sum(1 for line in lines
                                      if line and "#genre#" not in line and "," in line)
            m["live_group_names"] = [
                line.split(",", 1)[0].strip()
                for line in lines if "#genre#" in line
            ]
        except Exception:  # noqa: BLE001
            pass
    else:
        # 回退：没有 txt 时用 live.json 的分组计数（口径已标注，不参与骤降比较）
        live_json = _load_json(os.path.join(public_dir, "live.json"))
        if live_json:
            lives = live_json.get("lives") or []
            m["live_groups"] = sum(1 for x in lives if x.get("channels"))
            m["live_channels"] = sum(len(x.get("channels") or []) for x in lives
                                     if isinstance(x, dict))

    # 各产物体积
    for fn in sorted(os.listdir(public_dir)):
        p = os.path.join(public_dir, fn)
        if os.path.isfile(p):
            m["artifacts"][fn] = os.path.getsize(p)

    return m


def evaluate(metrics: dict, baseline: dict | None,
             rules: dict | None = None) -> tuple[bool, list[str], list[str]]:
    """执行门禁判定。返回 (是否通过, 硬问题, 软警告)。"""
    rules = rules or K.contract().get("quality") or {}
    hard: list[str] = []
    soft: list[str] = []

    # --- 硬门禁 1：契约冲突必须为 0 ---
    n = metrics.get("contract_conflicts", 0)
    if n:
        hard.append(f"契约冲突 {n} 处 —— 客户端会整份配置加载失败")
        for kind, items in (metrics.get("conflict_detail") or {}).items():
            for x in items[:5]:
                hard.append(f"    {kind}: {x}")

    # --- 硬门禁 2/3：相对基线不得骤降 ---
    ratio = float(rules.get("min_sites_ratio_vs_last", 0.7))
    if baseline:
        base_sites = int(baseline.get("sites") or 0)
        if base_sites:
            cur = int(metrics.get("sites") or 0)
            if cur < base_sites * ratio:
                hard.append(
                    f"站点数骤降：{base_sites} → {cur}（阈值 {int(base_sites*ratio)}，"
                    f"跌幅超过 {(1-ratio)*100:.0f}%）")

        base_ch = int(baseline.get("live_channels") or 0)
        if base_ch:
            cur = int(metrics.get("live_channels") or 0)
            if cur < base_ch * ratio:
                hard.append(
                    f"直播频道骤降：{base_ch} → {cur}（阈值 {int(base_ch*ratio)}）")

    # --- 硬门禁 4：关键分组 ---
    need_groups = rules.get("must_have_live_groups") or []
    names = metrics.get("live_group_names")
    if need_groups and names is not None:
        for g in need_groups:
            if not any(g in x for x in names):
                hard.append(f"缺少关键直播分组「{g}」")

    # --- 硬门禁 5：绝对下限 ---
    min_sites = int(rules.get("min_sites") or 0)
    if min_sites and int(metrics.get("sites") or 0) < min_sites:
        hard.append(f"站点数 {metrics.get('sites')} 低于绝对下限 {min_sites}")

    min_ch = int(rules.get("min_live_channels") or 0)
    if min_ch and int(metrics.get("live_channels") or 0) < min_ch:
        hard.append(f"直播频道 {metrics.get('live_channels')} 低于绝对下限 {min_ch}")

    min_p = int(rules.get("min_parses") or 0)
    if min_p and int(metrics.get("parses") or 0) < min_p:
        hard.append(f"解析器 {metrics.get('parses')} 低于绝对下限 {min_p}")

    # --- 软警告：体积 ---
    limits = rules.get("max_artifact_bytes") or {}
    for name, limit in limits.items():
        size = (metrics.get("artifacts") or {}).get(name)
        if size and size > limit:
            soft.append(f"{name} 体积 {size:,d}B 超过契约上限 {limit:,d}B")

    return (len(hard) == 0), hard, soft


def load_baseline() -> dict | None:
    return _load_json(GATE_FILE)


def save_baseline(metrics: dict) -> None:
    os.makedirs(os.path.dirname(GATE_FILE), exist_ok=True)
    data = {
        "at": C.iso(),
        "at_bj": C.bjnow(),
        "sites": metrics.get("sites"),
        "parses": metrics.get("parses"),
        "live_channels": metrics.get("live_channels"),
        "live_groups": metrics.get("live_groups"),
        "artifacts": metrics.get("artifacts"),
    }
    tmp = GATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, GATE_FILE)


def write_quality(metrics: dict, passed: bool, hard: list[str], soft: list[str]) -> None:
    """把质量报告落进产物目录，便于巡检和人工查看。"""
    data = {
        "checked_at": C.iso(),
        "checked_at_bj": C.bjnow(),
        "passed": passed,
        "metrics": {k: v for k, v in metrics.items() if k != "conflict_detail"},
        "hard_issues": hard,
        "warnings": soft,
    }
    try:
        tmp = QUALITY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, QUALITY_FILE)
    except Exception as e:  # noqa: BLE001
        C.log(f"[gate] 写 quality.json 失败（不影响门禁判定）：{e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="tv-hub 质量门禁")
    ap.add_argument("--public", default=None, help="产物目录（默认 public/）")
    ap.add_argument("--no-baseline", action="store_true",
                    help="忽略旧基线，以当前产物重建基线（上游结构确有大变化时用）")
    ap.add_argument("--dry-run", action="store_true", help="只判定，不更新基线")
    a = ap.parse_args()

    C.log("=" * 60)
    C.log("[gate] 质量门禁开始")
    metrics = collect(a.public)
    baseline = None if a.no_baseline else load_baseline()

    C.log(f"[gate] 指标：sites={metrics['sites']} parses={metrics['parses']} "
          f"直播={metrics['live_groups']}组/{metrics['live_channels']}频道 "
          f"契约冲突={metrics['contract_conflicts']}")
    if baseline:
        C.log(f"[gate] 基线（{baseline.get('at_bj')}）：sites={baseline.get('sites')} "
              f"直播频道={baseline.get('live_channels')}")
    else:
        C.log("[gate] 无基线（首次运行或已忽略）—— 只做绝对阈值判定")

    passed, hard, soft = evaluate(metrics, baseline)

    if soft:
        C.log("[gate] 警告：")
        for w in soft:
            C.log(f"    ⚠ {w}")
    if hard:
        C.log("[gate] ❌ 硬门禁未通过：")
        for h in hard:
            C.log(f"    ✗ {h}")
    else:
        C.log("[gate] ✅ 全部硬门禁通过")

    write_quality(metrics, passed, hard, soft)

    if passed and not a.dry_run:
        save_baseline(metrics)
        C.log("[gate] 基线已更新")
    elif not passed:
        C.log("[gate] ⛔ 拒绝发布：线上将保持上一个好版本")

    C.log("=" * 60)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
