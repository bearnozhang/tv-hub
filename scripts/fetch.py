#!/usr/bin/env python3
"""
fetch.py —— 抓取上游，写入 cache/，更新 cache/_state.json。

单上游流程：primary → fallback（按序）→ HTTP 状态检查 → 超时 → 解析 → 结构校验
失败时：不清空旧缓存，保留 last_success，记录 consecutive_failures / error。

用法：
  python scripts/fetch.py                # 抓全部启用上游
  python scripts/fetch.py --id ysc_single_agg
  python scripts/fetch.py --dry-run       # 只校验不落盘
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def fetch_one(src: dict, dflt: dict, dry_run: bool = False) -> dict:
    sid = src["id"]
    urls = [src["primary"]] + list(src.get("fallback") or [])
    rec: dict = {
        "id": sid,
        "name": src["name"],
        "type": src["type"],
        "repo": src.get("repo"),
        "path": src.get("path"),
        "status": "unknown",
        "last_attempt": C.iso(),
        "last_success": None,
        "consecutive_failures": 0,
        "used_url": None,
        "http_status": None,
        "bytes": 0,
        "sha256": None,
        "counts": {},
        "error": None,
        "warnings": [],
        "from_cache": False,
    }
    old = C.load_state()["sources"].get(sid, {})
    rec["last_success"] = old.get("last_success")
    rec["consecutive_failures"] = int(old.get("consecutive_failures", 0))

    try:
        body, status, used = C.fetch_first_ok(
            urls, timeout=dflt["timeout"], retries=dflt["retries"],
            user_agent=dflt["user_agent"], backoff=dflt["retry_backoff_seconds"])
        rec["http_status"] = status
        rec["used_url"] = used
        rec["bytes"] = len(body)
        rec["sha256"] = C.sha256_of(body)

        v = C.validate_payload(src, body)
        rec["counts"] = v.get("counts", {})
        rec["warnings"] = (v.get("warnings") or [])[:10]
        rec["shape"] = v.get("shape")
        if not v.get("ok"):
            raise C.FetchError(used, "结构校验失败: " + "; ".join(v.get("errors") or ["unknown"]))

        rec["status"] = "ok"
        rec["last_success"] = C.iso()
        rec["consecutive_failures"] = 0
        rec["error"] = None
        if not dry_run:
            C.ensure_dirs()
            with open(C.cache_path(sid), "wb") as f:
                f.write(body)
        C.log(f"  [OK]   {sid:20s} {status} {len(body):>9,d}B  {rec['counts']}  <- {used[:70]}")
    except C.FetchError as e:
        rec["used_url"] = e.url if e.url else (urls[0] if urls else None)
        rec["error"] = e.reason[:800]
        rec["consecutive_failures"] += 1
        cached = C.cache_path(sid)
        if os.path.exists(cached):
            rec["status"] = "stale"
            rec["from_cache"] = True
            try:
                with open(cached, "rb") as f:
                    cb = f.read()
                v = C.validate_payload(src, cb)
                rec["counts"] = v.get("counts", rec["counts"])
                rec["bytes"] = len(cb)
                rec["sha256"] = C.sha256_of(cb)
            except Exception:  # noqa: BLE001 - 缓存也坏了就只报错
                rec["counts"] = {}
        else:
            rec["status"] = "failed"
        C.log(f"  [{rec['status'].upper():6s}] {sid:20s} err={rec['error'][:110]}")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", action="append", default=None, help="只抓指定 id（可多次）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cfg = C.load_sources()
    dflt = C.defaults_of(cfg)
    srcs = C.enabled_sources(cfg)
    if a.id:
        want = set(a.id)
        srcs = [s for s in srcs if s["id"] in want]
        missing = want - {s["id"] for s in srcs}
        if missing:
            C.log(f"[FATAL] 未知 source id: {sorted(missing)}")
            return 2

    C.log(f"[fetch] 待抓取 {len(srcs)} 个上游  timeout={dflt['timeout']}s retries={dflt['retries']}")
    state = C.load_state()
    recs = []
    for s in srcs:
        rec = fetch_one(s, dflt, a.dry_run)
        state["sources"][s["id"]] = rec
        recs.append(rec)
    if not a.dry_run:
        state["last_run"] = C.iso()
        state["last_run_bj"] = C.bjnow()
        state.setdefault("runs", []).append({
            "at": C.iso(),
            "ok": sum(1 for r in recs if r["status"] == "ok"),
            "stale": sum(1 for r in recs if r["status"] == "stale"),
            "failed": sum(1 for r in recs if r["status"] == "failed"),
            "dry_run": a.dry_run,
        })
        state["runs"] = state["runs"][-60:]
        C.save_state(state)

    ok = sum(1 for r in recs if r["status"] == "ok")
    stale = sum(1 for r in recs if r["status"] == "stale")
    failed = sum(1 for r in recs if r["status"] == "failed")
    C.log(f"[fetch] 结果 ok={ok} stale={stale} failed={failed}")
    return 0 if (ok + stale) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())