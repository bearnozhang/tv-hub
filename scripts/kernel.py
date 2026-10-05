#!/usr/bin/env python3
"""kernel.py —— 客户端契约的加载与通用校验。

## 这是什么

把「FongMi/TV 系客户端（讴歌、影视仓）能接受的配置长什么样」这件事，
从散落的注释、记忆和硬编码里，收敛成**一份机器可读的契约**：
`contract/kernel.json`。

本模块是读取和运用这份契约的唯一入口。sanitize / gate / watchdog / tests
全部通过它判断"某个字段是否合法"，不再各自维护一份字段表。

## 为什么必须这样

客户端源码里这条链路是致命的：

    // Site.java
    public static Site objectFrom(JsonElement el, String spider) {
        try { Site s = App.gson().fromJson(el, Site.class); ... }
        catch (Exception e) { return new Site(); }   // key = null
    }

    // VodConfig.initSite
    Map<String, Site> items = Site.findAll().stream()
        .collect(Collectors.toMap(Site::getKey, Function.identity()));
        // HashMap.merge(null, v, fn) -> NullPointerException

**配置里只要有 1 个站点的字段类型不符，整份配置就加载失败。**

因此"类型是否相符"必须是被机器持续校验的事实，而不是靠人记住。

## 设计原则

- 纯逻辑：不依赖项目内其他模块，不产生副作用
- 契约变更只改 `contract/kernel.json`，本模块代码不动
- 判定结果附带"为什么"，便于排查时直接引用
"""
from __future__ import annotations

import json
import os
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT_PATH = os.path.join(ROOT, "contract", "kernel.json")

_cache: dict | None = None


def contract() -> dict:
    """加载契约（进程内缓存）。"""
    global _cache
    if _cache is None:
        with open(CONTRACT_PATH, encoding="utf-8-sig") as f:
            _cache = json.load(f)
    return _cache


def reload() -> dict:
    """强制重载（测试或契约热更新时用）。"""
    global _cache
    _cache = None
    return contract()


def type_name(v: Any) -> str:
    """把 Python 值映射成契约里使用的类型名。

    注意 bool 必须排在 int 前面 —— Python 里 bool 是 int 的子类。
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def _expected_types(spec: dict) -> dict[str, str]:
    """把契约里 {str:[...], int:[...], ...} 反转成 {字段名: 期望类型}。"""
    out: dict[str, str] = {}
    for tname in ("str", "int", "list", "dict", "bool"):
        for field in spec.get(tname) or []:
            out[field] = tname
    return out


def _type_ok(want: str, got: str) -> bool:
    if want == "str":
        return got == "str"
    if want == "int":
        return got == "int"
    if want == "list":
        return got == "list"
    if want == "dict":
        return got == "dict"
    if want == "bool":
        # 客户端用 boolean 字段，但很多配置写 0/1，gson 都能接受
        return got in ("bool", "int")
    return False


def _check_object(obj: Any, spec: dict, label: str) -> list[str]:
    """按契约校验一个对象（site / parse / live），返回问题列表。"""
    if not isinstance(obj, dict):
        return [f"{label} 不是对象（实际 {type_name(obj)}）"]

    expected = _expected_types(spec)
    problems: list[str] = []

    for k, v in obj.items():
        if v is None:
            continue
        want = expected.get(k)
        if want is None:
            continue          # 未知字段：gson 会忽略，无害
        got = type_name(v)
        if not _type_ok(want, got):
            problems.append(f"字段 {k} 期望 {want} 实际 {got}")

    # 必填字段
    key_field = (spec.get("key") or {}).get("field")
    for f in spec.get("required") or []:
        if f == key_field:
            continue          # key 字段单独判定（见下）
        if not obj.get(f):
            problems.append(f"必填字段 {f} 缺失或为空")

    return problems


def site_conflicts(site: Any) -> list[str]:
    """站点是否会让 Site.objectFrom 抛异常。"""
    return _check_object(site, contract()["site"], "site")


def parse_conflicts(p: Any) -> list[str]:
    """解析器是否会让 Parse.objectFrom 抛异常。"""
    return _check_object(p, contract()["parse"], "parse")


def live_conflicts(lv: Any) -> list[str]:
    """直播条目是否会让 Live.objectFrom 抛异常。"""
    return _check_object(lv, contract()["live"], "live")


def config_conflicts(data: dict, kinds: tuple[str, ...] = ("site", "parse", "live")) -> dict:
    """扫描一整份配置的契约冲突。

    返回 {"site": [(序号, key, 问题), ...], "parse": [...], "live": [...], "total": N}

    total 为 0 是**发布硬性前提** —— 只要 >0，客户端就会整份加载失败。
    """
    c = contract()
    out: dict[str, Any] = {"site": [], "parse": [], "live": [], "total": 0}

    def scan(kind: str, items: list, checker) -> None:
        spec = c[kind]
        key_field = (spec.get("key") or {}).get("field", "key")
        checker_map = {"site": site_conflicts, "parse": parse_conflicts, "live": live_conflicts}
        fn = checker_map[kind]
        for i, obj in enumerate(items or []):
            probs = fn(obj)
            if probs:
                key = obj.get(key_field) if isinstance(obj, dict) else None
                out[kind].append((i, key, "; ".join(probs)))
        out["total"] += len(out[kind])

    if "site" in kinds:
        scan("site", data.get("sites") or [], site_conflicts)
    if "parse" in kinds:
        scan("parse", data.get("parses") or [], parse_conflicts)
    if "live" in kinds:
        scan("live", data.get("lives") or [], live_conflicts)
    return out


def duplicate_keys(items: list, kind: str) -> list[str]:
    """找出空 key / 重复 key —— 同样会触发 toMap 的 NPE / IllegalStateException。"""
    spec = contract()[kind]
    key_cfg = spec.get("key") or {}
    field = key_cfg.get("field", "key")
    keys = [str(o.get(field) or "").strip() for o in (items or []) if isinstance(o, dict)]

    bad: list[str] = []
    if key_cfg.get("non_empty"):
        empties = sum(1 for k in keys if not k)
        if empties:
            bad.append(f"{empties} 条记录的 {field} 为空")
    if key_cfg.get("unique"):
        seen: set[str] = set()
        dups: set[str] = set()
        for k in keys:
            if k in seen:
                dups.add(k)
            seen.add(k)
        if dups:
            bad.append(f"重复 {field}: {sorted(dups)[:5]}")
    return bad


# --------------------------------------------------------------------------
# URL 规则（读契约里的 url 段）
# --------------------------------------------------------------------------

def url_encodable(u: Any) -> bool:
    """URL 是否通过客户端侧的编码校验。

    Android OkHttp 用 java.net.URI 解析；非 ASCII 域名（中文域名）会抛异常。
    Python 的 latin-1 编码判定与之等价，故用它复现。
    """
    if not isinstance(u, str) or not u:
        return False
    enc = (contract().get("url") or {}).get("must_encode", "latin-1")
    try:
        u.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def url_ok(u: Any) -> bool:
    """URL 能否被客户端直接使用。"""
    if not isinstance(u, str) or not u:
        return False
    rules = contract().get("url") or {}
    if not url_encodable(u):
        return False
    schemes = tuple(rules.get("schemes") or ["http", "https"])
    if not u.startswith(tuple(s + "://" for s in schemes)):
        return False          # 覆盖相对路径与非法 scheme
    for bad in rules.get("forbid_prefix") or []:
        if u.startswith(bad):
            return False
    return True


# --------------------------------------------------------------------------
# 文本规则（编码 / BOM）
# --------------------------------------------------------------------------

def text_conflicts(raw: bytes) -> list[str]:
    """按契约校验产物的字节层规范。"""
    rules = contract().get("text") or {}
    problems: list[str] = []
    if rules.get("bom") is False and raw[:3] == b"\xef\xbb\xbf":
        problems.append("文件带 UTF-8 BOM（Android org.json 会视为非法字符）")
    if rules.get("trailing_newline") and raw and not raw.endswith(b"\n"):
        problems.append("文件末尾缺少换行")
    try:
        raw.decode(rules.get("encoding", "utf-8"))
    except UnicodeDecodeError as e:
        problems.append(f"编码不是 {rules.get('encoding', 'utf-8')}: {e}")
    return problems


def describe() -> str:
    """契约摘要（人读）。"""
    c = contract()
    lines = [
        f"契约版本 {c['version']}　适用 {c['applies_to']}",
        f"  核实时间 {c['verified_at']}（对照 {c['verified_against']}）",
        f"  site  必填 {(c['site'].get('required') or [])}  字段 {len(_expected_types(c['site']))} 个",
        f"  parse 必填 {(c['parse'].get('required') or [])}",
        f"  live  必填 {(c['live'].get('required') or [])}",
        f"  url   编码 {c['url'].get('must_encode')}　禁止前缀 {c['url'].get('forbid_prefix')}",
        f"  text  BOM={c['text'].get('bom')}  换行={c['text'].get('newline')}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if "--describe" in sys.argv or len(sys.argv) == 1:
        print(describe())
        raise SystemExit(0)
    if "--self-test" in sys.argv:
        # 用契约里记录的历史故障样本做自检
        cases = [
            ({"key": "a", "name": "A", "type": 0, "categories": "电影,电视剧"}, True,
             "categories 为字符串（实测 22 处）"),
            ({"key": "b", "name": "B", "type": 3, "api": "http://x/y.js",
              "ext": {"host": "http://h:1"}}, True,
             "ext 为对象（实测 13 处）"),
            ({"key": "c", "name": "C", "type": 0,
              "categories": ["电影"], "ext": "x"}, False, "合法站点"),
        ]
        bad = 0
        for obj, should_conflict, desc in cases:
            probs = site_conflicts(obj)
            got = bool(probs)
            mark = "OK " if got == should_conflict else "FAIL"
            if got != should_conflict:
                bad += 1
            print(f"  [{mark}] {desc}: {probs}")
        print("自检通过" if bad == 0 else f"自检失败 {bad} 项")
        raise SystemExit(1 if bad else 0)
