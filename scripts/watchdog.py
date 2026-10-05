#!/usr/bin/env python3
"""watchdog.py —— 站在用户视角，从外部验证线上产物是否真的可用。

## 和 gate.py 的分工

| | 时机 | 看什么 | 回答 |
|---|---|---|---|
| `gate.py` | 发布**前** | 本地产物 | 「这份东西值不值得发？」 |
| `watchdog.py` | 发布**后** | **线上**产物 | 「用户现在拿到的东西能用吗？」 |

「本地构建通过」不等于「线上能用」：Cloudflare 可能没部署成功、
Worker 可能改写错 Content-Type、线上可能还停在旧版本。
只有从外部真的拉一次，才知道。

## 检查项（全部读 contract/kernel.json）

1. 必需地址可达：`/tv`、`/live`、`/spider.jar`
2. `/tv` 通过客户端契约校验 —— 有类型冲突就是致命故障
3. 内容量达标 —— 站点数 / 直播频道数不低于绝对下限
4. 依赖项 Content-Type 正确 —— spider.jar 必须是 java-archive，
   否则 TVBox 内核会拒绝加载（曾因 image/png 被拒）
5. 更新时间新鲜 —— 超过 `max_stale_hours` 未更新则提示补触发

退出码：0 健康 / 1 有异常 / 2 用法错误
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kernel as K      # noqa: E402

DEFAULT_BASE = "http://tv.bearno1.dpdns.org"

_ctx = ssl.create_default_context()
try:
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE
except Exception:  # noqa: BLE001
    pass


def fetch(url: str, timeout: int = 60, retries: int = 2) -> tuple[bytes | None, dict, str]:
    """拉取一个 URL。返回 (内容, 响应头, 错误信息)。"""
    last = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tv-hub-watchdog/1.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
                return r.read(), dict(r.headers), ""
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code < 500:
                break          # 4xx 不用重试
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:80]}"
        if attempt < retries:
            time.sleep(3)
    return None, {}, last


def _ct_ok(actual: str | None, expect: str) -> bool:
    return bool(actual) and expect.lower() in actual.lower()


def check(base: str, timeout: int = 60) -> dict:
    """执行全部巡检，返回结构化报告。"""
    rules = K.contract().get("watchdog") or {}
    quality = K.contract().get("quality") or {}
    ctype_expect = rules.get("expect_content_type") or {}

    report: dict = {
        "base": base,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": [],
        "errors": [],
        "warnings": [],
    }

    def add(name: str, ok: bool, detail: str, required: bool = True) -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail,
                                 "required": required})
        if not ok:
            (report["errors"] if required else report["warnings"]).append(f"{name}: {detail}")

    # ---- 1. 必需地址可达 ----
    bodies: dict[str, bytes] = {}
    for path in rules.get("required_paths") or ["/tv", "/live", "/spider.jar"]:
        body, headers, err = fetch(base + path, timeout)
        if body is None:
            add(f"可达 {path}", False, err or "无响应")
            continue
        bodies[path] = body
        expect = ctype_expect.get(path)
        ct = headers.get("Content-Type")
        if expect and not _ct_ok(ct, expect):
            add(f"可达 {path}", False,
                f"Content-Type 为 {ct}，期望包含 {expect}")
            continue
        add(f"可达 {path}", True, f"{len(body):,d} B  CT={ct}")

    # ---- 2. /tv 契约校验（核心）----
    if "/tv" in bodies:
        try:
            data = json.loads(bodies["/tv"].decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            add("契约 /tv", False, f"JSON 解析失败: {e}")
            data = None
        if data is not None:
            conf = K.config_conflicts(data)
            sites = len(data.get("sites") or [])
            if conf["total"]:
                sample = []
                for kind in ("site", "parse", "live"):
                    for i, key, prob in conf[kind][:3]:
                        sample.append(f"#{i} {key}: {prob}")
                add("契约 /tv", False,
                    f"{conf['total']} 处类型冲突（客户端会整份加载失败）: " + "; ".join(sample))
            else:
                add("契约 /tv", True, f"类型冲突 0，sites={sites}")
            report["metrics"] = {"sites": sites,
                                 "parses": len(data.get("parses") or [])}

            min_sites = int(quality.get("min_sites") or 0)
            if min_sites and sites < min_sites:
                add("内容量 /tv", False, f"sites={sites} 低于下限 {min_sites}")

            dup = K.duplicate_keys(data.get("sites") or [], "site")
            if dup:
                add("契约 /tv key 唯一性", False, "; ".join(dup))

            raw = bodies["/tv"]
            if raw[:3] == b"\xef\xbb\xbf":
                add("编码 /tv", False, "产物带 UTF-8 BOM（Android org.json 视为非法字符）")

    # ---- 3. /live 内容量 ----
    if "/live" in bodies:
        text = bodies["/live"].decode("utf-8-sig", errors="replace")
        lines = text.splitlines()
        groups = sum(1 for x in lines if "#genre#" in x)
        chans = sum(1 for x in lines if x and "#genre#" not in x and "," in x)
        report.setdefault("metrics", {})["live_groups"] = groups
        report["metrics"]["live_channels"] = chans
        min_ch = int(quality.get("min_live_channels") or 0)
        if min_ch and chans < min_ch:
            add("内容量 /live", False, f"频道数 {chans} 低于下限 {min_ch}")
        else:
            add("内容量 /live", True, f"{groups} 组 / {chans} 频道")
        need = quality.get("must_have_live_groups") or []
        names = [x.split(",", 1)[0].strip() for x in lines if "#genre#" in x]
        for g in need:
            if not any(g in x for x in names):
                add("关键分组 /live", False, f"缺少「{g}」")

    # ---- 4. 可选地址（失败只警告）----
    for path in rules.get("optional_paths") or []:
        body, headers, err = fetch(base + path, timeout)
        add(f"可选 {path}", body is not None, err or f"{len(body):,d} B", required=False)

    # ---- 5. 新鲜度 ----
    max_stale = float(rules.get("max_stale_hours") or 0)
    if max_stale:
        body, _, _ = fetch(base + "/status.json", timeout)
        if body:
            try:
                st = json.loads(body.decode("utf-8-sig"))
                gen = st.get("generated_at")
                if gen:
                    t = time.mktime(time.strptime(gen, "%Y-%m-%dT%H:%M:%SZ"))
                    age_h = (time.time() - t) / 3600.0
                    report["age_hours"] = round(age_h, 1)
                    if age_h > max_stale:
                        add("新鲜度", False,
                            f"距今 {age_h:.1f} 小时未更新（阈值 {max_stale}h）—— "
                            f"可能需要补触发构建",
                            required=False)
                    else:
                        add("新鲜度", True, f"距今 {age_h:.1f} 小时")
            except Exception as e:  # noqa: BLE001
                add("新鲜度", False, f"status.json 解析失败: {e}", required=False)

    report["healthy"] = len(report["errors"]) == 0
    return report


def check_channels(timeout: int = 45) -> dict:
    """逐个探测契约里定义的所有分发通道。

    为什么要单独做这一件事：
    TVBox 社区的经验里，「接口失效」的第一大原因是**通道问题**而非内容问题
    （域名被墙、CDN 抽风、第三方镜像挂了）。与其让用户挨个试，
    不如直接给出「现在哪条活着、哪条内容最新」的实测结果。

    同时校验两件事 —— 光「能下载」不够：
      1. 内容能否解析成合法配置
      2. Content-Type 是否规范（社区反馈：校验 CT 的客户端会拒绝 text/plain）
    """
    ch = K.contract().get("channels") or {}
    out: dict = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "selection_rule": ch.get("selection_rule", ""),
        "config": [],
        "live": [],
    }

    for kind in ("config", "live"):
        for item in ch.get(kind) or []:
            url = item["url"]
            body, headers, err = fetch(url, timeout=timeout)
            ct = headers.get("Content-Type")
            rec: dict = {
                "order": item.get("order"),
                "role": item.get("role"),
                "name": item.get("name"),
                "url": url,
                "why": item.get("why", ""),
                "ok": body is not None,
                "bytes": len(body) if body else 0,
                "content_type": ct,
            }
            # Content-Type 是否规范，要按通道类型分别判断：
            #   点播配置 → 必须是 application/json
            #   直播源   → text/plain（txt 形态）或 application/json（lives 数组）都合法
            if kind == "config":
                rec["ct_standard"] = bool(ct) and "json" in ct.lower()
            else:
                low = (ct or "").lower()
                rec["ct_standard"] = bool(ct) and ("text/plain" in low or "json" in low)
            if body is None:
                rec["error"] = err
            elif kind == "config":
                try:
                    data = json.loads(body.decode("utf-8-sig"))
                    rec["sites"] = len(data.get("sites") or [])
                    rec["json_ok"] = True
                except Exception as e:  # noqa: BLE001
                    rec["json_ok"] = False
                    rec["ok"] = False
                    rec["error"] = "JSON 解析失败: " + str(e)[:60]
            else:
                text = body.decode("utf-8-sig", errors="replace")
                rec["lines"] = len(text.splitlines())
                rec["groups"] = sum(1 for x in text.splitlines() if "#genre#" in x)
            out[kind].append(rec)
    return out


def render_channels(rep: dict) -> str:
    lines = ["分发通道实测（内容 + Content-Type 双重校验）", "=" * 74]
    for kind, label in (("config", "点播配置"), ("live", "直播源")):
        rows = rep.get(kind) or []
        if not rows:
            continue
        lines.append("")
        lines.append("【" + label + "】")
        for r in rows:
            if not r["ok"]:
                mark = "❌"
                detail = r.get("error", "不可达")
            else:
                if kind == "config":
                    detail = (str(r.get("sites")) + " 站  "
                              + ("" if r.get("json_ok") else "JSON异常  "))
                else:
                    detail = str(r.get("groups")) + " 组 / " + str(r.get("lines")) + " 行"
                mark = "✅" if r.get("ct_standard", True) else "⚠️"
                if not r.get("ct_standard", True):
                    detail += "  ← Content-Type 为 " + str(r.get("content_type")) + "（部分客户端会拒绝）"
                detail += "  " + str(round(r["bytes"] / 1024)) + "KB"
            lines.append("  " + mark + " [" + str(r.get("order")) + "] "
                         + str(r.get("name")) + "  " + detail)
            lines.append("        " + r["url"])
    lines.append("")
    lines.append("选择规则：" + (rep.get("selection_rule") or ""))
    return chr(10).join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="tv-hub 线上巡检")
    ap.add_argument("--base", default=DEFAULT_BASE, help="线上基址")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--json", action="store_true", help="输出 JSON（CI 用）")
    ap.add_argument("--channels", action="store_true",
                    help="检查契约里定义的所有分发通道")
    a = ap.parse_args()

    if a.channels:
        rep = check_channels(a.timeout)
        if a.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            print(render_channels(rep))
        bad = [r for kind in ("config", "live") for r in rep[kind] if not r["ok"]]
        return 0 if len(bad) < 2 else 1

    rep = check(a.base.rstrip("/"), a.timeout)

    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"巡检目标：{rep['base']}")
        print("-" * 66)
        for c in rep["checks"]:
            mark = "✅" if c["ok"] else ("❌" if c["required"] else "⚠ ")
            print(f"  {mark} {c['name']:22s} {c['detail']}")
        if rep.get("metrics"):
            m = rep["metrics"]
            print("-" * 66)
            print(f"  指标：sites={m.get('sites')} parses={m.get('parses')} "
                  f"直播={m.get('live_groups')}组/{m.get('live_channels')}频道")
        print("-" * 66)
        if rep["healthy"]:
            print("  ✅ 线上健康")
        else:
            print(f"  ❌ 发现 {len(rep['errors'])} 项异常")
            for e in rep["errors"]:
                print(f"      {e}")
    return 0 if rep["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
