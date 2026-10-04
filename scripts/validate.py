#!/usr/bin/env python3
"""
validate.py —— 校验缓存产物与最终配置。

两种模式：
  1) 校验 cache/ 里每个上游的已落盘数据（--scope cache）
  2) 校验最终 public/tv.json、public/live.json、public/status.json（--scope output，默认）

用法：
  python scripts/validate.py
  python scripts/validate.py --scope cache
  python scripts/validate.py --strict
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def check_site(site: dict, i: int) -> list[str]:
    errs: list[str] = []
    if not isinstance(site, dict):
        return [f"sites[{i}] 不是对象"]
    if not (site.get("key") or site.get("name")):
        errs.append(f"sites[{i}] 缺 key 和 name")
    t = site.get("type", 0)
    # 上游实测存在字符串型 type（如 "3"），merge 已归一化为 int；此处容忍但提示
    if isinstance(t, str):
        if t.strip().lstrip("-").isdigit():
            pass
        else:
            errs.append(f"sites[{i}]({site.get('key')}) type 非法: {t!r}")
    elif not isinstance(t, int):
        errs.append(f"sites[{i}]({site.get('key')}) type 非整数: {t!r}")
    if t in (1, "1") and not site.get("api"):
        errs.append(f"sites[{i}]({site.get('key')}) type=1 但缺 api")
    if "api" in site and not isinstance(site["api"], (str, list)):
        errs.append(f"sites[{i}]({site.get('key')}) api 类型异常")
    if "ext" in site and not isinstance(site["ext"], (str, dict)):
        errs.append(f"sites[{i}]({site.get('key')}) ext 类型异常")
    return errs


def validate_tv_output(data: dict, strict: bool = False) -> dict:
    errs: list[str] = []
    warns: list[str] = []
    if not isinstance(data, dict):
        return {"ok": False, "errors": ["tv.json 顶层不是对象"]}
    for f in ("sites", "parses", "lives", "flags"):
        if f in data and not isinstance(data[f], list):
            errs.append(f"tv.json.{f} 不是数组")
    sites = data.get("sites")
    if not isinstance(sites, list) or not sites:
        errs.append("tv.json.sites 缺失或为空 —— 这不是可用的配置")
        return {"ok": False, "errors": errs, "warnings": warns}
    for i, s in enumerate(sites):
        errs.extend(check_site(s, i))
        if len(errs) > 200:
            errs.append("...（错误过多，已截断）")
            break
    for i, p in enumerate(data.get("parses") or []):
        if not isinstance(p, dict):
            errs.append(f"parses[{i}] 不是对象")
        elif not p.get("name"):
            errs.append(f"parses[{i}] 缺 name")
    # lives 有两种合法形态：引用型 {name,url} / 分组型 {name,channels:[{name,urls:[]}]}
    for i, l in enumerate(data.get("lives") or []):
        if not isinstance(l, dict):
            errs.append(f"lives[{i}] 不是对象")
            continue
        chans = l.get("channels")
        if isinstance(chans, list):
            for j, c in enumerate(chans):
                if not isinstance(c, dict) or not c.get("name"):
                    errs.append(f"lives[{i}].channels[{j}] 缺 name")
                elif not (c.get("urls") or c.get("url")):
                    errs.append(f"lives[{i}].channels[{j}] 缺 urls/url")
        elif not l.get("url"):
            errs.append(f"lives[{i}] 既无 channels 也无 url")
        if not l.get("name"):
            errs.append(f"lives[{i}] 缺 name")
    for i, f in enumerate(data.get("flags") or []):
        if not isinstance(f, str):
            errs.append(f"flags[{i}] 不是字符串（TVBox flags 应为字符串数组）")
    if not data.get("spider"):
        warns.append("tv.json 无 spider 字段（若所有站点都靠 type=1 则正常）")
    keys = [s.get("key") for s in sites if isinstance(s, dict)]
    dup = len(keys) - len(set(keys))
    if dup:
        warns.append(f"存在 {dup} 个重复 key")
    if strict and warns:
        errs.extend(f"[strict] {w}" for w in warns)
    return {"ok": not errs, "errors": errs[:60], "warnings": warns,
            "counts": {"sites": len(sites),
                       "parses": len(data.get("parses") or []),
                       "lives": len(data.get("lives") or []),
                       "flags": len(data.get("flags") or [])}}


def validate_live_output(data: dict, strict: bool = False) -> dict:
    errs: list[str] = []
    warns: list[str] = []
    if not isinstance(data, dict):
        return {"ok": False, "errors": ["live.json 顶层不是对象"]}
    lives = data.get("lives")
    if not isinstance(lives, list) or not lives:
        return {"ok": False, "errors": ["live.json.lives 缺失或为空"], "warnings": warns}
    n_ch = 0
    for i, l in enumerate(lives):
        if not isinstance(l, dict):
            errs.append(f"lives[{i}] 不是对象")
            continue
        chans = l.get("channels")
        if isinstance(chans, list):
            n_ch += len(chans)
            for j, c in enumerate(chans):
                if not isinstance(c, dict) or not (c.get("urls") or c.get("url")):
                    errs.append(f"lives[{i}].channels[{j}] 缺 urls/url")
        elif l.get("url"):
            n_ch += 1
        else:
            errs.append(f"lives[{i}] 既无 channels 也无 url")
        if not l.get("name"):
            errs.append(f"lives[{i}] 缺 name")
    if strict and warns:
        errs.extend(f"[strict] {w}" for w in warns)
    return {"ok": not errs, "errors": errs[:60], "warnings": warns,
            "counts": {"groups": len(lives), "channels": n_ch}}


def validate_status(data: dict) -> dict:
    errs: list[str] = []
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        return {"ok": False, "errors": ["status.json 缺少 sources 数组"]}
    required = ("id", "status", "last_attempt", "consecutive_failures")
    for s in data["sources"]:
        for f in required:
            if f not in s:
                errs.append(f"status.sources[{(s.get('id'))}] 缺字段 {f}")
    return {"ok": not errs, "errors": errs[:40], "warnings": [],
            "counts": {"sources": len(data["sources"])}}


def validate_subscription(data: dict) -> dict:
    """订阅清单校验：必须是 {urls:[{name,url}]}，且每条 URL 可解析。
    这是用户唯一需要填的地址，坏了整条链路就断了。"""
    errs: list[str] = []
    warns: list[str] = []
    if not isinstance(data, dict):
        return {"ok": False, "errors": ["subscriptions.json 顶层不是对象"], "warnings": []}
    urls = data.get("urls")
    if not isinstance(urls, list) or not urls:
        return {"ok": False, "errors": ["subscriptions.json 缺少非空 urls 数组"], "warnings": []}
    base = ""
    for i, u in enumerate(urls):
        if not isinstance(u, dict):
            errs.append(f"urls[{i}] 不是对象"); continue
        if not u.get("name"):
            errs.append(f"urls[{i}] 缺 name")
        url = u.get("url", "")
        if not url.startswith(("http://", "https://")):
            errs.append(f"urls[{i}] url 非法: {url[:60]}")
        elif url.endswith("/tv.json"):
            base = url[:-len("/tv.json")]
    if not base:
        warns.append("清单里没有指向 tv.json 的聚合配置条目")
    return {"ok": not errs, "errors": errs[:20], "warnings": warns,
            "counts": {"urls": len(urls), "base": base or "-"}}


def scope_cache() -> dict:
    cfg = C.load_sources()
    state = C.load_state()
    report: dict[str, dict] = {}
    bad = 0
    for s in C.enabled_sources(cfg):
        sid = s["id"]
        p = C.cache_path(sid)
        if not os.path.exists(p):
            report[sid] = {"ok": False, "errors": ["缓存文件不存在"], "counts": {}}
            bad += 1
            continue
        with open(p, "rb") as f:
            body = f.read()
        v = C.validate_payload(s, body)
        v["bytes"] = len(body)
        report[sid] = v
        if not v["ok"]:
            bad += 1
        rec = state["sources"].get(sid, {})
        C.log(f"  [{'OK ' if v['ok'] else 'BAD'}] {sid:20s} {len(body):>9,d}B {v.get('counts')} "
              f"(last_status={rec.get('status')})")
    report["_summary"] = {"ok": bad == 0, "errors": [] if bad == 0 else [f"{bad} 个上游校验不通过"],
                          "warnings": [], "counts": {"sources": len(report), "bad": bad}}
    return report


def scope_output(strict: bool = False) -> dict:
    report: dict[str, dict] = {}
    targets = [
        ("public/tv.json", lambda d: validate_tv_output(d, strict)),
        ("public/live.json", lambda d: validate_live_output(d, strict)),
        ("public/status.json", validate_status),
        ("public/subscriptions.json", validate_subscription),
    ]
    bad = 0
    for rel, fn in targets:
        p = os.path.join(C.ROOT, rel.replace("/", os.sep))
        if not os.path.exists(p):
            report[rel] = {"ok": False, "errors": ["文件不存在"], "counts": {}}
            bad += 1
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            report[rel] = {"ok": False, "errors": [f"JSON 解析失败: {e}"], "counts": {}}
            bad += 1
            continue
        r = fn(data)
        report[rel] = r
        if not r["ok"]:
            bad += 1
        C.log(f"  [{'OK ' if r['ok'] else 'BAD'}] {rel:22s} {r.get('counts')} "
              f"errors={len(r.get('errors') or [])} warnings={len(r.get('warnings') or [])}")
        for e in (r.get("errors") or [])[:5]:
            C.log(f"        - {e}")
    report["_summary"] = {"ok": bad == 0,
                          "errors": [] if bad == 0 else [f"{bad} 个产物校验不通过"],
                          "warnings": [], "counts": {"targets": len(targets), "bad": bad}}
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["cache", "output"], default="output")
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    C.log(f"[validate] scope={a.scope}")
    rep = scope_cache() if a.scope == "cache" else scope_output(a.strict)
    if a.scope == "output" and rep["_summary"]["ok"]:
        C.log("[validate] 补充校验 cache/ …")
        cr = scope_cache()
        rep["cache"] = cr
        if not cr["_summary"]["ok"]:
            rep["_summary"] = {"ok": False, "errors": ["cache 校验不通过"], "warnings": []}
        # 校验单仓配置：订阅清单里点进去的每一个都必须能用
        pdir = os.path.join(C.PUBLIC_DIR, "profiles")
        if os.path.isdir(pdir):
            C.log("[validate] 补充校验 public/profiles/ …")
            bad = []
            n = 0
            for fn in sorted(os.listdir(pdir)):
                if not fn.endswith(".json"):
                    continue
                n += 1
                fp = os.path.join(pdir, fn)
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        d = json.load(f)
                except Exception as e:  # noqa: BLE001
                    bad.append(f"{fn}: JSON 解析失败 {e}")
                    continue
                if "urls" in d:
                    rep.setdefault(f"public/profiles/{fn}", {})["ok"] = bool(d["urls"])
                    continue
                r = validate_tv_output(d)
                if not r["ok"]:
                    bad.append(f"{fn}: {len(r['errors'])} 处错误 {r['errors'][:2]}")
                else:
                    C.log(f"  [OK ] public/profiles/{fn:24s} {r['counts']}")
            rep["profiles"] = {"ok": not bad, "errors": bad[:10], "warnings": [],
                               "counts": {"files": n, "bad": len(bad)}}
            if bad:
                rep["_summary"] = {"ok": False, "errors": [f"{len(bad)} 个单仓配置不可用"],
                                    "warnings": []}
    C.log(f"[validate] 结果: {json.dumps(rep['_summary'], ensure_ascii=False)}")
    return 0 if rep["_summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())