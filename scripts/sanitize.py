"""TVBox 内核契约消毒器 —— 消除会让客户端整份配置加载失败的字段类型冲突。

## 为什么需要这个（源码实证，不是推测）

FongMi/TV 系客户端（讴歌、影视仓等均由此衍生）加载配置时：

    // Site.java
    public static Site objectFrom(JsonElement element, String spider) {
        try {
            Site site = App.gson().fromJson(element, Site.class);   // ← 类型不符就抛
            ...
            return site.trans();
        } catch (Exception e) {
            return new Site();      // ← 静默返回空对象，key = null
        }
    }

    // VodConfig.initSite
    Map<String, Site> items = Site.findAll().stream()
        .collect(Collectors.toMap(Site::getKey, Function.identity()));
        // ↑ HashMap.merge(null, v, fn) 抛 NullPointerException

**结论：只要配置里有 1 个站点的字段类型不符合 Site 类的 gson 契约，
就会产生 null key，`Collectors.toMap` 直接抛 NPE，整份配置加载失败。**

这解释了「内容看起来完全正常（URL 合法、key 唯一）但客户端报解析失败」。

## 实测数据（2026-10-05，讴歌 6.0.9.3）

    /tv  (360KB, 1022站)  类型冲突 35  → 解析失败
    tv-standard (122KB)   类型冲突 22  → 解析失败
    hebi_tvbox (338KB)    类型冲突 35  → 解析失败
    ysc_single_agg (26KB) 类型冲突  0  → 可正常加载
    tv-lite (55KB)        类型冲突  0  → 可正常加载

冲突模式只有两类：
    categories: str（应为 list）  —— 22 处
    ext:        dict（应为 str）  —— 13 处

## 消毒规则

| 字段 | Site 契约 | 遇到不符时 |
|---|---|---|
| key/name/api/jar/click/playUrl | str | 非字符串 → 尝试转字符串，失败则丢弃字段 |
| ext | str | dict/list → JSON 序列化为字符串（保留信息） |
| type/hide/indexs/timeout/searchable/changeable/quickSearch/danmaku | int | str 数字 → 转 int；否则丢弃字段 |
| categories | list[str] | 字符串 → 按 ,／，/ 空格 拆分 |
| header | dict[str,str] | 值非字符串 → 转字符串；否则丢弃 |
| style | dict | 非 dict → 丢弃 |
| selected | bool | 其他真值 → 转 bool |

另外还要保证 `key` 非空且全局唯一 —— 空 key 同样会让 toMap 抛 NPE，
重复 key 抛 IllegalStateException。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 字段类型表统一从 contract/kernel.json 读取 —— 本文件不维护第二份。
# 客户端（FongMi 系）升级时只改契约，不改这里。
import kernel as K

def _fields(spec: str, tname: str) -> tuple[str, ...]:
    """从契约里取某类对象的某类型字段名元组。

    例：_fields("site", "str") -> ("key","name","api","ext","jar","click","playUrl")
    字段表在 contract/kernel.json，不在本文件重复维护。
    """
    return tuple((K.contract().get(spec) or {}).get(tname) or [])


def _as_int(v: Any) -> int | None:
    """尽力转 int。字符串数字可以救，其余放弃。"""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
        m = re.search(r"[+-]?\d+", s)
        if m:
            return int(m.group())
    return None


def _as_str(v: Any) -> str | None:
    """尽力转 str。dict/list 用 JSON 序列化（保留信息，且类型正确）。"""
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        except Exception:  # noqa: BLE001
            return None
    if isinstance(v, (int, float, bool)):
        return str(v)
    return None


def _as_str_list(v: Any) -> list[str] | None:
    if isinstance(v, list):
        out = [_as_str(x) for x in v]
        out = [x for x in out if x]
        return out
    if isinstance(v, str):
        parts = [p.strip() for p in re.split(r"[,，、;；/\s]+", v)]
        parts = [p for p in parts if p]
        return parts
    return None


def clean_site(site: Any) -> dict | None:
    """把一个站点对象对齐到 Site 契约。返回 None 表示该站点无法救活，应丢弃。"""
    if not isinstance(site, dict):
        return None
    strs = _fields("site", "str")
    ints = _fields("site", "int")
    out: dict = {}

    for k, v in site.items():
        if v is None:
            continue
        if k in strs:
            s = _as_str(v)
            if s is not None:
                out[k] = s
        elif k in ints:
            i = _as_int(v)
            if i is not None:
                out[k] = i
        elif k == "categories":
            lst = _as_str_list(v)
            if lst:
                out[k] = lst
        elif k == "header":
            if isinstance(v, dict):
                h = {}
                for hk, hv in v.items():
                    hs = _as_str(hv)
                    if hs is not None:
                        h[str(hk)] = hs
                if h:
                    out[k] = h
        elif k == "style":
            if isinstance(v, dict):
                out[k] = v
        elif k == "selected":
            out[k] = bool(v)
        else:
            # ★ 未知字段：**丢弃**（2026-10-05 修正）。
            #   原注释假设「gson 会忽略未知字段，客户端反正不读」——
            #   这对讴歌/影视仓（FongMi 系）成立，但**主流 TVBox 官方版
            #   （手机端）会因为未知字段直接判「解析配置失败」**。
            #   上游夹带的私有字段很多（reference、discovery_decoded_works、
            #   中文 key「类型」、title/lang/genre、id/isActive、order_num、
            #   playurl（小写）……），必须在这里一次性拦掉。
            #   原则：宁可少带字段，也不要带客户端不认识的东西。
            continue

    # key 必须存在且非空，否则 toMap 会因 null key / 重复而崩
    key = (out.get("key") or "").strip()
    if not key:
        return None
    out["key"] = key
    if not (out.get("name") or "").strip():
        out["name"] = key
    # type 必须有值：实测有站点缺 type；补 0（XML）为客户端默认
    if not isinstance(out.get("type"), int):
        out["type"] = 0
    return out


def clean_parse(p: Any) -> dict | None:
    if not isinstance(p, dict):
        return None
    strs = _fields("parse", "str")
    ints = _fields("parse", "int")
    out: dict = {}
    for k, v in p.items():
        if v is None:
            continue
        if k in strs:
            s = _as_str(v)
            if s is not None:
                out[k] = s
        elif k in ints:
            i = _as_int(v)
            if i is not None:
                out[k] = i
        elif k == "header":
            if isinstance(v, dict):
                out[k] = {str(a): _as_str(b) for a, b in v.items() if _as_str(b) is not None}
        else:
            out[k] = v
    if not (out.get("name") or "").strip() or not (out.get("url") or "").strip():
        return None
    return out


def clean_live(lv: Any) -> dict | None:
    """Live 契约：name 必须非空且不重复（initLive 里同样有 toMap）。"""
    if not isinstance(lv, dict):
        return None
    out: dict = {}
    for k, v in lv.items():
        if k == "type":
            i = _as_int(v)
            out[k] = i if i is not None else 0
        elif k in _fields("live", "str"):
            s = _as_str(v)
            if s is not None:
                out[k] = s
        elif k == "channels":
            if isinstance(v, list):
                chans = []
                for c in v:
                    if not isinstance(c, dict):
                        continue
                    cc: dict = {}
                    for ck, cv in c.items():
                        if ck == "urls" and isinstance(cv, list):
                            us = [_as_str(u) for u in cv]
                            us = [u for u in us if u and u.startswith(("http://", "https://"))]
                            if not us:
                                continue
                            cc["urls"] = us
                        elif isinstance(cv, (str, int, bool)):
                            cc[ck] = cv
                    if cc.get("name") and cc.get("urls"):
                        chans.append(cc)
                if chans:
                    out[k] = chans
        else:
            out[k] = v
    if not (out.get("name") or "").strip():
        return None
    if not out.get("url") and not out.get("channels"):
        return None
    return out


def sanitize_config(data: dict) -> tuple[dict, dict]:
    """对一份完整 TVBox 配置做契约消毒。返回 (消毒后的配置, 统计)。"""
    stats = {"sites_dropped": 0, "sites_fixed": 0, "sites_dedup": 0,
             "parses_dropped": 0, "lives_dropped": 0}

    sites_in = data.get("sites") or []
    seen: set[str] = set()
    sites_out: list[dict] = []
    for s in sites_in:
        c = clean_site(s)
        if c is None:
            stats["sites_dropped"] += 1
            continue
        if c["key"] in seen:
            stats["sites_dedup"] += 1
            continue
        seen.add(c["key"])
        if c != s:
            stats["sites_fixed"] += 1
        sites_out.append(c)
    data = dict(data)
    data["sites"] = sites_out

    if isinstance(data.get("parses"), list):
        ps = []
        pseen: set[str] = set()
        for p in data["parses"]:
            c = clean_parse(p)
            if c is None:
                stats["parses_dropped"] += 1
                continue
            if c["name"] in pseen:
                continue
            pseen.add(c["name"])
            ps.append(c)
        data["parses"] = ps

    if isinstance(data.get("lives"), list):
        ls = []
        lseen: set[str] = set()
        for lv in data["lives"]:
            c = clean_live(lv)
            if c is None:
                stats["lives_dropped"] += 1
                continue
            if c["name"] in lseen:
                continue
            lseen.add(c["name"])
            ls.append(c)
        data["lives"] = ls

    return data, stats


def _iter_json_targets(paths: list[str]) -> list[str]:
    """展开目录，只保留「TVBox 配置」形态的 JSON（含 sites 字段）。

    status.json / subscriptions.json / sub.json 等结构文件会被自动跳过 ——
    它们不是配置，没有 sites 字段。
    """
    import os
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for dp, _, fs in os.walk(p):
                for fn in sorted(fs):
                    if fn.endswith(".json"):
                        out.append(os.path.join(dp, fn))
        else:
            out.append(p)
    return out


def main() -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="TVBox 配置契约消毒")
    ap.add_argument("paths", nargs="*", help="JSON 文件或目录（目录会递归）")
    ap.add_argument("--inplace", action="store_true", help="原地覆盖")
    ap.add_argument("--check", action="store_true",
                    help="只检查：存在契约冲突即返回非零（用于 CI 把关）")
    ap.add_argument("--quiet", action="store_true", help="只输出改动项")
    a = ap.parse_args()

    changed = 0
    for p in _iter_json_targets(a.paths):
        if not os.path.exists(p):
            print(f"  [跳过] {p} 不存在")
            continue
        raw = open(p, "rb").read()
        try:
            d = json.loads(raw.decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            print(f"  [跳过] {p} 非 JSON: {e}")
            continue
        if not isinstance(d, dict) or "sites" not in d:
            if not a.quiet:
                print(f"  [非配置] {os.path.basename(p)}")
            continue

        before = len(d.get("sites") or [])
        fixed, st = sanitize_config(d)
        after = len(fixed["sites"])
        dirty = st["sites_fixed"] + st["sites_dropped"] + st["sites_dedup"]
        if dirty == 0 and before == after:
            if not a.quiet:
                print(f"  [干净] {os.path.basename(p):28s} sites={after}")
            continue

        changed += 1
        print(f"  [已修] {os.path.basename(p):28s} sites {before}→{after}  "
              f"修正={st['sites_fixed']} 丢弃={st['sites_dropped']} 去重={st['sites_dedup']}")
        if a.inplace:
            # 与 common.write_json 的约定保持一致：JSON 产物不带 UTF-8 BOM。
            # Android org.json/JSONObject 会把 BOM 当非法字符，直接导致解析失败。
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                json.dump(fixed, f, ensure_ascii=False, indent=2)
                f.write("\n")

    print(f"[sanitize] 共处理 {changed} 个需要修正的配置")
    if a.check and changed:
        print("[sanitize] ❌ 存在会让客户端整份配置加载失败的字段类型冲突")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
