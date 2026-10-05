#!/usr/bin/env python3
"""
build.py —— 一键构建：抓取 → 校验 → 合并 → 产出 public/tv.json、live.json、
subscriptions.json、status.json、index.html。

硬性规则：
  若所有上游都失败且本地无历史成功缓存 → 构建失败（非零退出），
  绝不允许产出一个「看起来正常但实际为空」的 tv.json。

用法：
  python scripts/build.py
  python scripts/build.py --skip-fetch     # 只用现有缓存重建
  python scripts/build.py --allow-empty    # 允许空结果（仅调试）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import fetch as F  # noqa: E402
import merge as M  # noqa: E402
import validate as V  # noqa: E402

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tv-hub · 配置状态</title>
<style>
  ol.ch{margin:.4em 0 0;padding-left:1.4em;line-height:1.55}
  ol.ch li{margin:.35em 0}
  ol.ch li.prim{font-weight:600}
  ol.ch .role{display:inline-block;min-width:1.5em;text-align:center;border:1px solid currentColor;
    border-radius:3px;padding:0 .25em;font-size:.82em;vertical-align:1px}
  ol.ch .why{opacity:.62;font-size:.9em}
  ol.ch li.rule{border-top:1px dashed currentColor;padding-top:.4em;margin-top:.5em;
    opacity:.75;font-size:.92em;list-style:none;margin-left:-1.4em}
:root{color-scheme:dark}
body{margin:0;padding:32px 20px;background:#0d1117;color:#e6edf3;
 font:14px/1.6 -apple-system,"Segoe UI",Microsoft YaHei,sans-serif}
.wrap{max-width:920px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}p.sub{color:#8b949e;margin:0 0 24px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}
.kpi{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 16px}
.kpi b{display:block;font-size:24px;font-weight:600}
.kpi span{color:#8b949e;font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:500;font-size:12px}
code{background:#161b22;padding:1px 5px;border-radius:4px;font-size:12px;
 word-break:break-all;color:#79c0ff}
a{color:#58a6ff;text-decoration:none}a:hover{text-decoration:underline}
.ok{color:#3fb950}.stale{color:#d29922}.failed{color:#f85149}
.err{color:#8b949e;font-size:12px;margin-top:3px;word-break:break-all}
footer{margin-top:28px;color:#8b949e;font-size:12px;border-top:1px solid #21262d;padding-top:14px}
</style></head><body><div class="wrap">
<h1>tv-hub · 配置状态</h1>
<p class="sub">聚合 · 校验 · 去重 · 失败回退 ｜ 生成于 __UPDATED__</p>
<div class="kpis">
__KPIS__
</div>
<table><thead><tr><th>上游</th><th>类型</th><th>状态</th><th>数量</th><th>使用地址</th><th>最后成功</th><th>连续失败</th></tr></thead>
<tbody>__ROWS__</tbody></table>
<footer>
<h2>订阅地址</h2>
<p class="sub">按顺序用：上面那条能开就不要换 —— 社区经验反复验证，频繁追新源反而更不稳定。</p>
<ol class="ch">__CHANNELS__</ol>
数据来源版权归各原始作者所有，本仓库仅做聚合与镜像。
</footer>
</div></body></html>
"""


def kpi(label: str, value: object) -> str:
    return f'<div class="kpi"><b>{value}</b><span>{label}</span></div>'


def render_channels_html() -> str:
    """生成「订阅地址」列表 —— 读 contract/kernel.json 的 channels 段，地址不写死在这里。

    放到看板上的理由：社区里最常见的求助就是「接口又失效了，求新地址」。
    把主+备+选择规则直接摆在用户面前，比让他到处找省事。
    """
    try:
        import kernel as K
        ch = K.contract().get("channels") or {}
    except Exception:  # noqa: BLE001
        return "<li>（契约未加载）</li>"
    items = []
    for it in ch.get("config") or []:
        role = "主" if it.get("role") == "primary" else "备"
        cls = ' class="prim"' if it.get("role") == "primary" else ""
        items.append(
            '<li' + cls + '><span class="role">' + role + '</span> <b>'
            + str(it.get("name", "")) + '</b><br><code>'
            + str(it.get("url", "")) + '</code><br><span class="why">'
            + str(it.get("why", "")) + '</span></li>')
    rule = ch.get("selection_rule") or ""
    if rule:
        items.append('<li class="rule">' + rule + '</li>')
    return "".join(items)


def render_index(status: dict) -> str:
    s = status["summary"]
    kpis = "".join([
        kpi("TVBox 站点", s.get("unique_sites", 0)),
        kpi("解析器 parses", s.get("unique_parses", 0)),
        kpi("直播分组", s.get("live_groups", 0)),
        kpi("直播频道", s.get("live_channels", 0)),
        kpi("直播档位", len(s.get("live_tiers") or [])),
        kpi("成功上游", f"{s.get('ok', 0)}/{s.get('total_sources', 0)}"),
        kpi("去重掉站点", s.get("sites_deduped", 0)),
    ])
    rows = []
    for r in status["sources"]:
        err = f'<div class="err">{r["error"][:180]}</div>' if r.get("error") else ""
        cache_badge = ' <span class="stale">缓存</span>' if r.get("from_cache") else ""
        rows.append(
            f'<tr><td><b>{r["name"]}</b><br><code>{r["id"]}</code></td>'
            f'<td>{r["type"]}</td>'
            f'<td class="{r["status"]}">{r["status"]}{cache_badge}</td>'
            f'<td>{json.dumps(r.get("counts") or {}, ensure_ascii=False)}</td>'
            f'<td><code>{(r.get("used_url") or "-")[:120]}</code>{err}</td>'
            f'<td>{r.get("last_success") or "-"}</td>'
            f'<td>{r.get("consecutive_failures", 0)}</td></tr>')
    base = status.get("base_url") or "https://<你的域名>/"
    return (INDEX_HTML.replace("__KPIS__", kpis)
            .replace("__ROWS__", "\n".join(rows))
            .replace("__CHANNELS__", render_channels_html())
            .replace("__UPDATED__", status["generated_at_bj"])
            .replace("__BASE__", base))


def build_status(cfg: dict, state: dict, stats: dict) -> dict:
    srcs = C.enabled_sources(cfg)
    rows = []
    ok = stale = failed = 0
    for s in srcs:
        rec = state["sources"].get(s["id"], {})
        st = rec.get("status", "never")
        ok += st == "ok"
        stale += st == "stale"
        failed += st in ("failed", "never")
        rows.append({
            "id": s["id"], "name": s["name"], "type": s["type"],
            "repo": s.get("repo"), "path": s.get("path"),
            "status": st,
            "last_attempt": rec.get("last_attempt"),
            "last_success": rec.get("last_success"),
            "consecutive_failures": int(rec.get("consecutive_failures", 0)),
            "used_url": rec.get("used_url"),
            "http_status": rec.get("http_status"),
            "bytes": rec.get("bytes", 0),
            "sha256": rec.get("sha256"),
            "counts": rec.get("counts", {}),
            "from_cache": bool(rec.get("from_cache")),
            "error": rec.get("error"),
            "warnings": rec.get("warnings", []),
        })
    return {
        "generated_at": C.iso(),
        "generated_at_bj": C.bjnow(),
        "base_url": os.environ.get("TVHUB_BASE_URL", "").strip() or M.base_url(),
        "summary": {
            "total_sources": len(srcs), "ok": ok, "stale": stale, "failed": failed,
            "raw_sites": stats.get("raw_sites", 0),
            "unique_sites": stats.get("unique_sites", 0),
            "sites_deduped": stats.get("sites_deduped", 0),
            "unique_parses": stats.get("unique_parses", 0),
            "unique_lives": stats.get("unique_lives", 0),
            "live_groups": stats.get("live_groups", 0),
            "live_channels": stats.get("live_channels", 0),
            "live_url_deduped": stats.get("live_url_deduped", 0),
            "subscriptions": stats.get("subscriptions", 0),
            "sources_used": stats.get("sources_used", []),
            "sources_skipped": stats.get("sources_skipped", []),
            "per_source_sites": stats.get("per_source_sites", {}),
            "hashes": stats.get("sha256", {}),
            # 策展统计（筛掉了什么、探测结果）—— 让看板能显示「源质量」
            "curate": {k: v for k, v in stats.items() if k.startswith("curate_")},
        },
        "sources": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--allow-empty", action="store_true", help="调试用：允许空结果")
    a = ap.parse_args()

    C.log("=" * 64)
    C.log("[build] tv-hub 构建开始")
    C.log("=" * 64)

    cfg = C.load_sources()
    if not a.skip_fetch:
        C.log("\n--- 阶段 1/4：抓取 ---")
        dflt = C.defaults_of(cfg)
        state = C.load_state()
        for s in C.enabled_sources(cfg):
            rec = F.fetch_one(s, dflt)
            state["sources"][s["id"]] = rec
        state["last_run"] = C.iso()
        state["last_run_bj"] = C.bjnow()
        state.setdefault("runs", []).append({
            "at": C.iso(),
            "ok": sum(1 for r in state["sources"].values() if r.get("status") == "ok"),
        })
        state["runs"] = state["runs"][-60:]
        C.save_state(state)
    else:
        C.log("\n--- 阶段 1/4：抓取（跳过，使用现有缓存）---")

    state = C.load_state()

    def _usable(s: dict) -> bool:
        p = C.cache_path(s["id"])
        if not os.path.exists(p):
            return False
        with open(p, "rb") as fh:
            return bool(C.validate_payload(s, fh.read()).get("ok"))

    usable = [s for s in C.enabled_sources(cfg) if _usable(s)]
    C.log(f"[build] 可用缓存上游 {len(usable)}/{len(C.enabled_sources(cfg))}")
    if not usable and not a.allow_empty:
        C.log("[build][FATAL] 全部上游不可用且无历史成功缓存 —— 拒绝生成空配置，构建失败。")
        return 3

    C.log("\n--- 阶段 2/4：缓存结构校验 ---")
    cr = V.scope_cache()
    C.log(f"[build] cache 校验: ok={cr['_summary']['ok']} bad={cr['_summary']['counts'].get('bad')}")

    C.log("\n--- 阶段 3/4：合并 + 去重 ---")
    mr = M.merge(build=True)
    stats = mr["stats"]
    C.log(json.dumps({k: v for k, v in stats.items()
                      if k not in ("sources_used", "sources_skipped", "per_source_sites")},
                     ensure_ascii=False, indent=2))

    if not mr["ok"] and not a.allow_empty:
        C.log("[build][FATAL] 合并后 unique_sites=0 —— 拒绝产出空配置，构建失败。")
        return 4

    C.log("\n--- 阶段 4/4：落盘 ---")
    status = build_status(cfg, state, stats)
    # _headers 由 Cloudflare 消费，必须保留（build 只覆盖产物，不会删它，
    # 但为防万一显式校验并告警）
    hdr = os.path.join(C.PUBLIC_DIR, "_headers")
    if not os.path.exists(hdr):
        C.log("[build][WARN] public/_headers 缺失，Cloudflare 会按后缀猜 Content-Type，"
              "可能导致 .txt/.webp 订阅被解析失败")
    C.write_json(os.path.join(C.PUBLIC_DIR, "status.json"), status)
    html = render_index(status)
    with open(os.path.join(C.PUBLIC_DIR, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(html)

    C.log("[build] 产物：")
    for f in ("tv.json", "live.json", "subscriptions.json", "status.json", "index.html"):
        p = os.path.join(C.PUBLIC_DIR, f)
        if not os.path.exists(p):
            C.log(f"  {f:20s} <缺失>")
            continue
        with open(p, "rb") as fh:
            digest = C.sha256_of(fh.read())
        C.log(f"  {f:20s} {os.path.getsize(p):>10,d} B  {digest[:16]}")

    C.log("\n--- 产物校验 ---")
    vr = V.scope_output()
    if not vr["_summary"]["ok"]:
        C.log("[build][FATAL] 产物校验不通过")
        return 5

    C.log(f"\n[build] ✅ 完成：sites={stats['unique_sites']} "
          f"(去重 {stats['sites_deduped']}) parses={stats['unique_parses']} "
          f"lives={stats['unique_lives']}+{stats['live_groups']}组/{stats['live_channels']}频道")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())