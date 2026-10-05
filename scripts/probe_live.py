#!/usr/bin/env python3
"""probe_live.py —— 直播源实测与精简。

## 为什么需要它
直播源和点播源的根本差别：**直播源可以被硬验证**（就是一堆 m3u8/ts 地址，
发个请求就知道死活），而点播的 `csp_` 源只能在客户端跑、服务端验不了。
所以直播这一块能做到「只交付验证过能连的」。

## 判定
- 只用 GET + `Range: bytes=0-1023`（很多直播源不支持 HEAD）
- **不跟随重定向**（一是避免把请求打到无关域名，二是 3xx 本身说明源不直给）
- 认为「活着」的证据（任一）：
    * body 以 `#EXTM3U` 开头        → m3u8 索引
    * body 首字节是 0x47            → MPEG-TS 包（同步字节）
    * Content-Type 含 mpegurl/m3u8  → 明确的 HLS 类型
- 频道级判定：**只要该频道有一个 URL 活着，这个频道就可交付**

## 用法
    python scripts/probe_live.py --in public/live.txt --report out.json
    python scripts/probe_live.py --in public/live.txt --write-public public/live.txt
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

UA = "okhttp/3.12.0"
_OPENER = None


def _opener():
    global _OPENER
    if _OPENER is None:
        import urllib.request

        class NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        _OPENER = build_opener(NoRedir)
    return _OPENER


def probe(url: str, timeout: float = 7.0) -> dict:
    """探测一个直播 URL。返回 {ok, code, kind, ms, err}。"""
    t0 = time.time()
    try:
        req = Request(url, headers={"User-Agent": UA,
                                    "Accept-Encoding": "identity",
                                    "Range": "bytes=0-1023"})
        with _opener().open(req, timeout=timeout) as r:
            body = r.read(1024)
            code = r.status
            ct = (r.headers.get("Content-Type") or "").lower()
    except HTTPError as e:
        code = e.code
        try:
            body = e.read(256)
        except Exception:
            body = b""
        ct = (e.headers.get("Content-Type") or "").lower()
    except (URLError, OSError, ValueError) as e:
        return {"ok": False, "code": None, "kind": "err",
                "ms": int((time.time() - t0) * 1000), "err": type(e).__name__}

    ms = int((time.time() - t0) * 1000)
    kind = "?"
    ok = False
    if body[:7] == b"#EXTM3U":
        kind, ok = "m3u8", True
    elif body[:1] == b"\x47":
        kind, ok = "ts", True
    elif "mpegurl" in ct or "m3u" in ct:
        kind, ok = "m3u8", True
    elif code == 206 and body:
        kind, ok = "partial", True
    if code and code >= 400:
        ok = False
        kind = "http%d" % code
    return {"ok": ok, "code": code, "kind": kind, "ms": ms, "err": ""}


def parse_live(path: str) -> list:
    """解析 txt 直播源 → [(group, name, url)]，保持原顺序。"""
    out = []
    group = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            if s.endswith("#genre#"):
                group = s.replace(",#genre#", "").replace("#genre#", "").strip()
                continue
            if "," not in s:
                continue
            name, _, url = s.rpartition(",")
            name, url = name.strip(), url.strip()
            if url.startswith(("http://", "https://")):
                out.append((group, name, url))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--report")
    ap.add_argument("--write-public")
    ap.add_argument("--timeout", type=float, default=7.0)
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--keep-per-channel", type=int, default=3,
                    help="每个频道最多保留几个「已验证」的 URL")
    a = ap.parse_args()

    items = parse_live(a.src)
    print("[probe_live] 解析到 %d 条频道记录" % len(items))
    urls = sorted({u for _, _, u in items})
    print("[probe_live] 去重后 %d 个 URL，并发 %d 探测…" % (len(urls), a.workers))

    res = {}
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(probe, u, a.timeout): u for u in urls}
        for fu in as_completed(futs):
            u = futs[fu]
            try:
                res[u] = fu.result()
            except Exception as e:
                res[u] = {"ok": False, "code": None, "kind": "err",
                          "ms": 0, "err": type(e).__name__}
            done += 1
            if done % 500 == 0:
                print("  …%d/%d  (%.0fs)" % (done, len(urls), time.time() - t0))
    ok = sum(1 for v in res.values() if v["ok"])
    print("[probe_live] 完成：%d 个 URL，可达 %d 个（%.0f%%），耗时 %.0fs"
          % (len(urls), ok, ok / max(1, len(urls)) * 100, time.time() - t0))

    chans = {}
    for g, n, u in items:
        chans.setdefault((g, n), [])
        if u not in chans[(g, n)]:
            chans[(g, n)].append(u)
    dead_ch = [("%s/%s" % (g, n)) for (g, n), us in chans.items()
               if not any(res.get(u, {}).get("ok") for u in us)]
    alive_ch = len(chans) - len(dead_ch)
    print("[probe_live] 频道 %d 个：至少一个源可达 %d 个，全部不可达 %d 个"
          % (len(chans), alive_ch, len(dead_ch)))
    if dead_ch:
        print("  全死频道样例:", "、".join(dead_ch[:12]))

    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "total_urls": len(urls), "ok_urls": ok,
                       "channels": len(chans), "alive_channels": alive_ch,
                       "dead_channels": dead_ch,
                       "results": res}, f, ensure_ascii=False)
        print("[probe_live] 报告写入 %s" % a.report)

    if a.write_public:
        keep = a.keep_per_channel
        lines = []
        last_group = None
        seen = set()
        wrote_ch = 0
        for g, n, _u in items:
            key = (g, n)
            if key in seen:
                continue
            good = [x for x in chans.get(key, []) if res.get(x, {}).get("ok")]
            if not good:
                continue                      # 全死频道直接不写
            seen.add(key)
            if g != last_group:
                lines.append("%s,#genre#" % g)
                last_group = g
            for uu in good[:keep]:
                lines.append("%s,%s" % (n, uu))
            wrote_ch += 1
        with open(a.write_public, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("[probe_live] 写入 %s：%d 个频道 / %d 行"
              % (a.write_public, wrote_ch, len(lines)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
