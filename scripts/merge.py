#!/usr/bin/env python3
"""
merge.py —— 去重 + 合并。

去重策略（按稳定字段优先级，不因来源 URL 不同而重复保留同一站点）：
  sites : key → (name, api归一化) → (name, type) → 完整内容哈希
  parses: name → 完整内容哈希
  lives : name → (name, 首个 url)
  channels: name+url 精确 → url

结构策略：不同结构的 JSON 不粗暴拼接。
  tvbox 源合并 sites/parses/lives/flags/spider/标量；
  live_txt / live_m3u 源合并为「按 group 归类的 channels」，
  再作为 group 条目追加到 lives（不塞进 sites）。
  subscription 源只产出订阅清单，不参与 sites 合并。

用法：
  python scripts/merge.py                # 输出合并统计
  python scripts/merge.py --stats-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

URL_SPLIT = re.compile(r"[,，]")


def norm_url(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()


def norm_api(a: object) -> str:
    if isinstance(a, list):
        return "|".join(norm_url(x) for x in a)
    return norm_url(str(a))


def norm_name(n: object) -> str:
    s = str(n or "").strip().lower()
    return re.sub(r"\s+", "", s)


def site_fingerprint(s: dict) -> str:
    """站点内容哈希（用于完全相同但无 key/name 稳定字段的情形）。"""
    keep = {k: s[k] for k in ("key", "name", "type", "api", "ext", "jar", "playUrl", "categories") if k in s}
    return C.sha256_of(json.dumps(keep, ensure_ascii=False, sort_keys=True))


def site_identity(s: dict) -> list[str]:
    """返回该站点的候选身份键，优先级从高到低。"""
    ids: list[str] = []
    key = s.get("key")
    if key:
        ids.append(f"key:{str(key).strip()}")
    nm, api = norm_name(s.get("name")), norm_api(s.get("api"))
    if nm and api:
        ids.append(f"na:{nm}|{api}")
    if nm:
        ids.append(f"n:{nm}|t{s.get('type', 0)}")
    ids.append(f"h:{site_fingerprint(s)}")
    return ids


class Deduper:
    """跨来源去重器：记录每个身份键归属的首个来源。"""

    def __init__(self) -> None:
        self.owner: dict[str, str] = {}
        self.dropped: dict[str, int] = {}
        self.kept: int = 0

    def accept(self, ident: list[str], src_id: str) -> bool:
        for i in ident:
            prev = self.owner.get(i)
            if prev is None:
                self.owner[i] = src_id
            else:
                if prev != src_id:
                    self.dropped[prev] = self.dropped.get(prev, 0) + 1
                return False
        self.kept += 1
        return True

    def count_for(self, src_id: str) -> int:
        return sum(1 for v in self.owner.values() if v == src_id)


def classify_lives(items: list[tuple[str, dict]], stats: dict) -> tuple[list[dict], list[dict]]:
    """上游实测存在两类异常，需分流而不是原样透传：
       A) lives 里混入「站点对象」（带 key/api/jar，type=3）→ 实为 site，应回填 sites
       B) url 为空字符串的占位条目→ 无效，直接丢弃
    返回 (合法 lives, 从 lives 中救出的 sites)。"""
    good: list[dict] = []
    rescued: list[dict] = []
    for src_id, l in items:
        if not isinstance(l, dict):
            stats["lives_invalid"] = stats.get("lives_invalid", 0) + 1
            continue
        has_ch = isinstance(l.get("channels"), list) and l["channels"]
        has_url = isinstance(l.get("url"), str) and l["url"].strip()
        # A) 站点对象误入 lives
        if not has_ch and not has_url and (l.get("api") or l.get("key") or l.get("jar")):
            rescued.append({"key": l.get("key") or f"live_misplaced_{len(rescued)}",
                            "name": l.get("name") or "未命名",
                            "type": l.get("type") if isinstance(l.get("type"), int) else (int(l["type"]) if str(l.get("type", "")).strip().isdigit() else 0),
                            "api": l.get("api"),
                            "ext": l.get("ext"),
                            "_rescued_from": "lives"})
            stats["lives_rescued_as_site"] = stats.get("lives_rescued_as_site", 0) + 1
            continue
        if not has_ch and not has_url:
            stats["lives_invalid"] = stats.get("lives_invalid", 0) + 1
            continue
        good.append((src_id, l))
    return good, rescued


def normalize_site(s: dict) -> dict:
    """上游实测存在字符串型 type（如 "3"）与缺失 type，统一归一化后再落盘，
    避免下游 TVBox 客户端因类型不一致而解析异常。返回新对象（不改原缓存）。"""
    out = dict(s)
    t = out.get("type", 0)
    if isinstance(t, str):
        t2 = t.strip()
        out["type"] = int(t2) if t2.lstrip("-").isdigit() else 0
    elif isinstance(t, bool):
        out["type"] = int(t)
    elif not isinstance(t, int):
        out["type"] = 0
    # api 为空串时视为缺失，避免下游误判为有效接口
    if out.get("api") == "":
        out.pop("api")
    # ext 实测存在三种形态：str（JSON 串）、dict（{k:url}）、
    # list（[{name,url}]，非TVBox 标准）。list 归一化为 dict，空值直接剔除。
    ext = out.get("ext")
    if isinstance(ext, list):
        conv: dict[str, str] = {}
        for it in ext:
            if isinstance(it, dict) and it.get("name") and it.get("url"):
                conv[str(it["name"])] = str(it["url"])
        out["ext"] = conv if conv else ""
    elif isinstance(ext, dict) and not ext:
        out["ext"] = ""
    # ext 为 null / 空 dict / 空 list 归一：直接剔除，避免下游拿到非标准值
    if not isinstance(out.get("ext"), (str, dict)) or not out.get("ext"):
        out.pop("ext", None)
    # 去掉本项目内部标记字段，不外泄到最终配置
    out.pop("_rescued_from", None)
    return out


def merge_sites(items: list[tuple[str, dict]], dd: Deduper, stats: dict) -> list[dict]:
    out: list[dict] = []
    for src_id, s in items:
        if not isinstance(s, dict):
            stats["sites_invalid"] = stats.get("sites_invalid", 0) + 1
            continue
        if not (s.get("key") or s.get("name")):
            stats["sites_invalid"] = stats.get("sites_invalid", 0) + 1
            continue
        s = normalize_site(s)
        if dd.accept(site_identity(s), src_id):
            out.append(s)
    return out


def merge_named(items: list[tuple[str, dict]], kind: str, dd: Deduper, stats: dict) -> list[dict]:
    """合并「对象数组」型字段（parses / lives 引用型）。
    每项必须有可识别标识（name），否则计入 invalid。"""
    out: list[dict] = []
    for src_id, p in items:
        if not isinstance(p, dict) or not p.get("name"):
            stats[f"{kind}_invalid"] = stats.get(f"{kind}_invalid", 0) + 1
            continue
        nm = norm_name(p["name"])
        if dd.accept([f"{kind}:{nm}", f"{kind}:h:{C.sha256_of(json.dumps(p, sort_keys=True, ensure_ascii=False))}"], src_id):
            out.append(p)
    return out


def merge_flags(items: list[tuple[str, object]], dd: Deduper, stats: dict) -> list[str]:
    """flags 实测为纯字符串数组（如 ["youku","优酷","优 酷"]），不是对象。
    去重时忽略空白与大小写，但保留原始写法（上游习惯全大写/中文）。"""
    out: list[str] = []
    for src_id, f in items:
        if not isinstance(f, str) or not f.strip():
            stats["flags_invalid"] = stats.get("flags_invalid", 0) + 1
            continue
        if dd.accept([f"flags:{f.strip().lower()}"], src_id):
            out.append(f)
    return out


def group_channels(channels: list[dict], src_id: str) -> dict[str, dict]:
    """把 channel 列表按 group 归类，同名频道聚合多备源。"""
    groups: dict[str, dict] = {}
    for c in channels:
        g = c.get("group") or "未分组"
        node = groups.setdefault(g, {"name": g, "type": 0, "group": g,
                                     "channels": {}, "from": set(), "logo": c.get("logo", "")})
        node["from"].add(src_id)
        name = str(c.get("name") or c.get("url")).strip()
        ch = node["channels"].setdefault(name, {"name": name, "urls": [], "sources": set()})
        u = c.get("url")
        if u and u not in ch["urls"]:
            ch["urls"].append(u)
        ch["sources"].add(src_id)
        if c.get("tvg_id"):
            ch["tvg_id"] = c["tvg_id"]
        if not node["logo"] and c.get("logo"):
            node["logo"] = c["logo"]
    return groups


# 产物对外基址。Cloudflare 部署后为自定义域，未部署时回落 GitHub raw。
# 可用环境变量 TVHUB_BASE_URL 覆盖。
DEFAULT_BASE_URL = "https://tv.bearno1.dpdns.org"


TIER_LIVE_CORE = ("央视", "卫视", "地方-", "港台", "其他")


def clean_live_entries(lives: list[dict]) -> tuple[list[dict], dict]:
    """清洗直播条目：剔除客户端无法使用的引用型。

    源码依据（FongMi/TV LiveConfig.initLive）：
      setLives(Live.objectFrom(...))
      Collectors.toMap(Live::getName, ...)   // name 空/重复会抛异常
    实测问题（上游脏数据）：
      - 70 个 url 是 ./lives/xxx.txt 相对路径 → 客户端无法解析
      - 49 个 url 指向 127.0.0.1 / localhost → 对客户端毫无意义
      - 空 name 会让 toMap 抛 IllegalStateException
    """
    stats = {"relative": 0, "localhost": 0, "noname": 0, "dupname": 0}
    seen: set[str] = set()
    out: list[dict] = []
    for x in lives:
        if not isinstance(x, dict):
            continue
        name = str(x.get("name") or "").strip()
        if not name:
            stats["noname"] += 1
            continue
        if name in seen:
            stats["dupname"] += 1
            continue
        url = str(x.get("url") or "").strip()
        chans = x.get("channels")
        if url and not url.startswith(("http://", "https://")):
            stats["relative"] += 1
            continue
        if url.startswith(("http://127.0.0.1", "http://localhost")):
            stats["localhost"] += 1
            continue
        if not url and not isinstance(chans, list):
            continue
        seen.add(name)
        y = dict(x)
        y["name"] = name
        out.append(y)
    return out, stats


# 直播配置键序：与实测可用的点播配置保持一致（lives 不在第 2 位）
LIVE_KEY_ORDER = ("spider", "wallpaper", "logo", "warningText", "proxy", "doh",
                  "hosts", "rules", "lives", "parses", "flags")


def write_live_tiers(live_out: list[dict], ref_lives: list[dict], spider: str,
                     scalars: dict) -> list[dict]:
    """产出直播分档配置。

    实测（讴歌 6.0.9.3）：客户端对 >100KB 的配置会解析失败，
    因此直播同样按体积分档，urls[0] 给最小最稳的一档。
    """
    written: list[dict] = []
    grp = [x for x in live_out if x.get("channels")]

    def ch_count(items: list[dict]) -> int:
        return sum(len(x.get("channels") or []) for x in items)

    # 核心台：央视 + 卫视（多备源、稳定性高）
    core = [x for x in grp if x["name"] in ("央视", "卫视")]
    # 地方台：按频道数降序，保证每一档都有实用价值
    local = sorted([x for x in grp if x["name"].startswith("地方-")],
                   key=lambda x: -ch_count([x]))
    other = [x for x in grp if x["name"] not in ("央视", "卫视")
             and not x["name"].startswith("地方-")]

    tiers = [
        ("live-mini.txt", core),                            # 仅央视+卫视，最小
        ("live-lite.txt", core + local[:5]),                 # 央视+卫视+5 个地方
        ("live-standard.txt", core + local[:20]),            # 央视+卫视+20 个地方
        ("live-full.txt", grp),                              # 全部真实频道分组
    ]
    # 备源收敛：实测卫视平均 20.4 个备源/频道（52 频道就1063 个 URL），
    # 体积与冗余都浪费。每频道保留 MAX_BACKUP 个备源即可（实测 3 个成功率已足够），
    # 仍保留多备源机制的价值（播放失败自动换源），但不至于撑爆客户端。
    def trim(items: list[dict], cap: int) -> list[dict]:
        out = []
        for g in items:
            if not g.get("channels"):
                out.append(g)
                continue
            chs = []
            for c in g["channels"]:
                urls = c.get("urls") or []
                if len(urls) > cap:
                    c = {**c, "urls": urls[:cap]}
                chs.append(c)
            out.append({**g, "channels": chs})
        return out

    # 备源上限：mini 档 2 个（最小体积），其余 3 个
    caps = {"live-mini.txt": 2, "live-lite.txt": 3,
            "live-standard.txt": 3, "live-full.txt": 3}

    for name, items in tiers:
        if not items:
            continue
        items = trim(items, caps.get(name, 3))
        obj: dict = {}
        for k in LIVE_KEY_ORDER:
            if k == "lives":
                obj["lives"] = items
            elif k in ("spider",) and spider:
                obj[k] = spider
            elif k in scalars and scalars[k] not in (None, "", [], {}):
                obj[k] = scalars[k]
        C.write_json(os.path.join(C.PUBLIC_DIR, name), obj)
        written.append({"name": name, "groups": len(items),
                        "channels": ch_count(items),
                        "bytes": os.path.getsize(os.path.join(C.PUBLIC_DIR, name))})
    return written


def base_url() -> str:
    """产物对外基址。

    必须防御未渲染的 GitHub Actions 表达式：workflow 里写过
    `${{ vars.X || 'https://.../${{ github.repository }}/...' }}`，
    嵌套表达式不会被二次展开，会被原样传进来，导致订阅里出现
    字面量 `${{ github.repository }}` → 所有线路 URL 失效。
    因此这里显式检测并回落到默认基址。
    """
    env = os.environ.get("TVHUB_BASE_URL", "").strip()
    if "${{" in env or "}}" in env or not env:
        return DEFAULT_BASE_URL
    if not env.startswith(("http://", "https://")):
        return DEFAULT_BASE_URL
    return env.rstrip("/")


def profile_sites_count(sid: str) -> int:
    """读已落盘的 profile，取真实站点数。
    不用 per_source_sites（那是「去重归属计数」，同key 被多源命中时只算给首个来源，
    各源数字相加会远大于实际唯一站点数）。"""
    p = os.path.join(C.PUBLIC_DIR, "profiles", f"{sid}.json")
    if not os.path.exists(p):
        return 0
    try:
        # 必须用 utf-8-sig：文件带 BOM（对齐已知可用配置），否则 json.load 抛异常
        with open(p, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:  # noqa: BLE001
        return 0
    return len(d.get("sites") or [])


def build_profiles(cfg: dict, per_source_sites: dict, unique_total: int = 0) -> list[dict]:
    """产出「单仓级」配置文件清单 —— 这才是订阅列表里该出现的条目。

    体积分档（实测讴歌 6.0.9.3 等 TVBox 系客户端）：
      老板实测 68KB 的配置能正常加载，1.6MB+ 的直接解析失败。
      因此按体积分档给出多档，让用户按客户端能力选择。
    """
    b = base_url()
    profiles: list[dict] = []
    total = unique_total or sum(per_source_sites.values())

    # ⚠️ 关键约束（源码实证：FongMi/TV VodConfig.parseDepot）
    #     parseDepot() 只执行 load(configs.get(0)) —— urls[0] 是App
    #     唯一会自动加载的条目，其余仅作为「App 内可手动切换」的列表存在。
    #     所以 urls[0] 必须是「一定能用且体积最小」的那一个，
    #     否则 App 启动即解析失败（老客户端加载大文件会挂）。
    #     实测（讴歌 6.0.9.3）：68KB 可用，136KB 以上失败。
    #     → urls[0] 固定为轻量版；全量版排后面，仅供手动切换。
    for tname, tcnt, tnote in (
        ("tv-lite.json", TIER_LITE[0], "体积最小，启动首选；老客户端兼容最好"),
        ("tv-standard.json", TIER_STD[0], "站点更多，兼容中等体积客户端"),
    ):
        profiles.append({
            "name": f"★ {tname.replace('.json', '')}（{tcnt} 站 · 兼容版）",
            "url": f"{b}/{tname}",
            "note": tnote,
            "kind": "aggregate-lite",
        })

    profiles.append({"name": f"tv-hub 全量聚合（{total} 站 · 体积大）",
                     "url": f"{b}/tv.json",
                     "note": "全部上游合并去重；老客户端加载大文件会失败，需手动切换到此",
                     "kind": "aggregate"})
    # 注意：不能用 per_source_sites 的归属计数判断是否收录——
    # hebi_vod 的计数是 0（站点全被 hebi_tvbox 抢占归属），
    # 但它作为独立单仓文件完全可用（4278 站）。
    # 判据改为「profile 文件已落盘且站点数 > 0」。
    for s in cfg["sources"]:
        sid = s["id"]
        if s["type"] not in ("tvbox", "subscription") or not s.get("enabled", True):
            continue
        if sid not in per_source_sites:
            continue
        cnt = profile_sites_count(sid)
        if not cnt:
            continue
        profiles.append({
            "name": f"{s['name']} · {cnt} 站",
            "url": f"{b}/profiles/{sid}.json",
            "note": (s.get("notes") or "")[:80],
            "kind": "single",
        })
    return profiles


TIER_LITE = (300, "lite")
TIER_STD = (1000, "standard")


def write_lite_tiers(sites: list[dict], parses: list[dict], flags: list,
                     spider: str, scalars: dict) -> list[str]:
    """产出体积分档配置。

    实测（讴歌 6.0.9.3）：68KB 配置可正常加载，1.6MB 以上直接解析失败。
    因此按**总体积**切档，且优先保留纯接口站点（type=0/1，不依赖 jar、体积小），
    其余位置用 type=3 补足 —— 纯接口只有 250 个，只靠它无法填满较大档位。
    """
    written: list[str] = []
    plain = [s for s in sites if s.get("type", 0) in (0, 1)]
    other = [s for s in sites if s.get("type", 0) not in (0, 1)]

    def pick(limit: int) -> list[dict]:
        out = plain[:limit]
        if len(out) < limit:
            out += other[: limit - len(out)]
        return out

    # 键序对齐实测可用的 ysc_single_agg（sites 不在第 2 位）：
    #   spider, wallpaper, logo, warningText, proxy, doh, hosts, rules,
    #   sites, lives, parses, flags
    KEY_ORDER = ("spider", "wallpaper", "logo", "warningText", "proxy", "doh",
                 "hosts", "rules", "sites", "lives", "parses", "flags")

    def ordered(o: dict) -> dict:
        out = {k: o[k] for k in KEY_ORDER if k in o}
        out.update({k: v for k, v in o.items() if k not in out})
        return out

    tiers = [("tv-lite.json", pick(TIER_LITE[0])),
             ("tv-standard.json", pick(TIER_STD[0]))]
    for name, ss in tiers:
        obj: dict = {"sites": ss}
        if spider:
            obj = {"spider": spider, **obj}
        for k in ("wallpaper", "logo", "proxy", "doh", "hosts", "rules"):
            if scalars.get(k) not in (None, "", [], {}):
                obj[k] = scalars[k]
        if parses:
            obj["parses"] = parses[:60]
        if flags:
            obj["flags"] = flags
        obj["lives"] = []
        C.write_json(os.path.join(C.PUBLIC_DIR, name), ordered(obj))
        written.append(name)
    return written


def write_profiles(cfg: dict, per_source_sites: dict) -> list[str]:
    """把每个上游的原始内容重新落盘为 profiles/<id>.json，供订阅清单分发。

    必须应用与主配置**完全相同**的清洗，否则用户切到单仓会踩到上游脏数据
    （实测：hebijunge 的 lives 里混有站点对象、ysc 有缺 name 的条目）。
    """
    out_dir = os.path.join(C.PUBLIC_DIR, "profiles")
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    stats: dict = {}
    for s in cfg["sources"]:
        sid = s["id"]
        if s["type"] not in ("tvbox", "subscription") or not s.get("enabled", True):
            continue
        src_p = C.cache_path(sid)
        if not os.path.exists(src_p):
            continue
        with open(src_p, "rb") as f:
            body = f.read()
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except Exception:  # noqa: BLE001
            continue

        if s["type"] == "subscription":
            # 多仓订阅原样转发（它本身就是给 App 切换用的清单）
            urls = data.get("urls")
            if isinstance(urls, list) and urls:
                C.write_json(os.path.join(out_dir, f"{sid}.json"),
                             {"urls": [u for u in urls if isinstance(u, dict) and u.get("url")]})
                written.append(sid)
            continue

        out: dict = {}
        for k in ("spider", "wallpaper", "logo", "warningText", "proxy", "doh",
                  "hosts", "rules", "version", "ad", "tihuan"):
            if k in data and data[k] not in (None, "", [], {}):
                out[k] = data[k]

        # 注意：这里必须用**独立的 Deduper**。
        # 主配置的 Deduper 跨源共享，hebi_tvbox 已抢占了全部 key 归属，
        # 若复用同一个，hebi_vod 的 sites 会被全部去重掉 → 产出空配置。
        out["sites"] = merge_sites([(sid, x) for x in (data.get("sites") or [])],
                                    Deduper(), stats)

        # lives 分类：站点对象分流回 sites，空 url 丢弃
        good_lives, rescued = classify_lives([(sid, x) for x in (data.get("lives") or [])], stats)
        if rescued:
            out["sites"] = out["sites"] + merge_sites(
                [(f"r:{r['key']}", r) for r in rescued], Deduper(), stats)
        out["lives"] = merge_named(good_lives, "lives", Deduper(), stats)

        out["parses"] = merge_named([(sid, x) for x in (data.get("parses") or [])],
                                    "parses", Deduper(), stats)
        out["flags"] = merge_flags([(sid, x) for x in (data.get("flags") or [])], Deduper(), stats)

        # 直播引用壳（sites=0）落盘后是「空配置」，客户端会直接报解析失败，
        # 不产出。它们的价值已被 merge 主流程吸收（live_groups）。
        if not out["sites"]:
            continue

        C.write_json(os.path.join(out_dir, f"{sid}.json"), out)
        written.append(sid)
    return written


def merge(build: bool = False) -> dict:
    """build=True 时把结果写入 public/；否则只统计。"""
    cfg = C.load_sources()
    state = C.load_state()
    src_by_id = {s["id"]: s for s in cfg["sources"]}

    sites_items: list[tuple[str, dict]] = []
    parses_items: list[tuple[str, dict]] = []
    lives_items: list[tuple[str, dict]] = []
    flags_items: list[tuple[str, object]] = []
    live_groups: dict[str, dict] = {}
    subscriptions: list[dict] = []
    scalars: dict[str, object] = {}
    spider = ""
    stats: dict = {"sources_used": [], "sources_skipped": [], "sites_invalid": 0,
                   "parses_invalid": 0, "lives_invalid": 0, "flags_invalid": 0}

    dd_sites, dd_parses, dd_lives, dd_flags = Deduper(), Deduper(), Deduper(), Deduper()

    for sid, s in src_by_id.items():
        if not s.get("enabled", True):
            stats["sources_skipped"].append({"id": sid, "reason": "enabled=false"})
            continue
        rec = state["sources"].get(sid, {})
        path = C.cache_path(sid)
        if not os.path.exists(path):
            stats["sources_skipped"].append({"id": sid, "reason": "无缓存（抓取失败且无历史）"})
            continue
        with open(path, "rb") as f:
            body = f.read()
        v = C.validate_payload(s, body)
        if not v.get("ok"):
            stats["sources_skipped"].append({"id": sid, "reason": "缓存结构校验失败",
                                             "errors": v.get("errors", [])[:3]})
            continue

        kind = s["type"]
        if kind == "subscription":
            data = json.loads(body.decode("utf-8-sig"))
            for u in data.get("urls", []):
                if isinstance(u, dict) and u.get("url"):
                    subscriptions.append({"name": u.get("name", ""), "url": u["url"],
                                          "via": sid})
            stats["sources_used"].append({"id": sid, "kind": kind,
                                          "counts": v.get("counts", {}), "status": rec.get("status")})
            continue

        if kind in ("live_txt", "live_m3u"):
            for gname, node in group_channels(v["channels"], sid).items():
                tgt = live_groups.setdefault(gname, node)
                for cname, ch in node["channels"].items():
                    cur = tgt["channels"].setdefault(cname, {"name": cname, "urls": [], "sources": set()})
                    for u in ch["urls"]:
                        if u not in cur["urls"]:
                            cur["urls"].append(u)
                    cur["sources"] |= ch["sources"]
                    if ch.get("tvg_id"):
                        cur["tvg_id"] = ch["tvg_id"]
                tgt["from"] |= node["from"]
            stats["sources_used"].append({"id": sid, "kind": kind,
                                          "counts": v.get("counts", {}), "status": rec.get("status")})
            continue

        # tvbox：按字段语义合并，绝不整对象覆盖
        data = json.loads(body.decode("utf-8-sig"))
        sites_items += [(sid, x) for x in (data.get("sites") or [])]
        parses_items += [(sid, x) for x in (data.get("parses") or [])]
        lives_items += [(sid, x) for x in (data.get("lives") or [])]
        flags_items += [(sid, x) for x in (data.get("flags") or [])]
        sp = data.get("spider")
        if isinstance(sp, str) and sp and not spider:
            spider = sp
        for f in ("wallpaper", "logo", "warningText", "proxy", "doh", "hosts", "rules",
                  "version", "ad", "tihuan"):
            if f in data and f not in scalars and data[f] not in (None, "", [], {}):
                scalars[f] = data[f]
        stats["sources_used"].append({"id": sid, "kind": kind,
                                      "counts": v.get("counts", {}), "status": rec.get("status")})

    sites = merge_sites(sites_items, dd_sites, stats)
    # lives 先分类；救出的站点对象追加到 sites 末尾一并去重
    # （必须放在原有 sites 之后：Deduper 以「首个出现的来源」为准，
    #  放前面会让 rescue 项抢占 key 归属，把真实站点误判为重复）
    good_lives, rescued = classify_lives(lives_items, stats)
    if rescued:
        sites += merge_sites([(f"rescued:{r['key']}", r) for r in rescued], dd_sites, stats)
        stats["sites_from_rescued_lives"] = len(rescued)
    parses = merge_named(parses_items, "parses", dd_parses, stats)
    lives = merge_named(good_lives, "lives", dd_lives, stats)
    flags = merge_flags(flags_items, dd_flags, stats)

    # live group → lives 条目（频道多的作为展开型 group）
    live_out: list[dict] = []
    ch_total = 0
    ch_dedup = 0
    seen_urls: set[str] = set()
    for gname, node in sorted(live_groups.items(), key=lambda kv: -len(kv[1]["channels"])):
        chans = []
        for cname, ch in sorted(node["channels"].items()):
            urls = ch["urls"]
            if len(urls) > 1:
                ch_dedup += 0
            for u in urls:
                if u in seen_urls:
                    ch_dedup += 1
                    continue
                seen_urls.add(u)
            chans.append({"name": cname, "urls": urls})
        if not chans:
            continue
        ch_total += len(chans)
        entry = {"name": node["name"], "type": 0, "group": node["name"], "channels": chans}
        if node.get("logo"):
            entry["logo"] = node["logo"]
        live_out.append(entry)

    raw_site_count = len(sites_items) + len(rescued)
    tv: dict = {"spider": spider}
    tv.update(scalars)
    tv["sites"] = sites
    tv["lives"] = lives + live_out
    tv["parses"] = parses
    tv["flags"] = flags

    stats.update({
        "raw_sites": raw_site_count,
        "unique_sites": len(sites),
        "sites_deduped": raw_site_count - len(sites),
        "raw_parses": len(parses_items), "unique_parses": len(parses),
        "raw_lives": len(lives_items), "unique_lives": len(lives),
        "live_groups": len(live_out), "live_channels": ch_total,
        "live_url_deduped": ch_dedup,
        "unique_flags": len(flags),
        "subscriptions": len(subscriptions),
        "per_source_sites": {sid: dd_sites.count_for(sid) for sid in {i for i, _ in sites_items}},
    })

    if not build:
        return {"ok": len(sites) > 0, "stats": stats, "tv": None}

    ensure = C.ensure_dirs()
    del ensure
    per_source = stats.get("per_source_sites", {})

    # 单仓级配置文件：让订阅清单里的每个条目都能直接打开
    written_profiles = write_profiles(cfg, per_source) if build else []
    tiers_written = write_lite_tiers(sites, parses, flags, spider, scalars) if build else []

    # 直播：清洗无效引用 + 体积分档（与点播同一套适配逻辑）
    clean_groups, live_clean_stats = clean_live_entries(live_out)
    clean_refs, ref_clean_stats = clean_live_entries(lives)
    live_tiers = write_live_tiers(clean_groups, clean_refs, spider, scalars) if build else []

    # 订阅清单：一个地址，App 内可切换多个仓
    profiles = build_profiles(cfg, per_source, len(sites))
    sub_obj = {
        "urls": [{"name": p["name"], "url": p["url"]} for p in profiles],
    }
    res = {
        "tv": C.write_json(os.path.join(C.PUBLIC_DIR, "tv.json"), tv),
        "live": C.write_json(os.path.join(C.PUBLIC_DIR, "live.json"), {
            "updated_at": C.bjnow(), "generated_at": C.iso(),
            "groups": len(clean_groups), "channels": ch_total,
            "lives": clean_groups + clean_refs,
        }),
        "subscriptions": C.write_json(os.path.join(C.PUBLIC_DIR, "subscriptions.json"), sub_obj),
    }
    # 直播订阅清单：与点播同构，urls[0] 为体积最小的档位
    live_sub = {"urls": [{"name": f"★ {t['name'].replace('.txt','')}（{t['groups']} 组 / {t['channels']} 频道）",
                          "url": f"{base_url()}/{t['name']}"} for t in live_tiers]}
    res["live_sub"] = C.write_json(os.path.join(C.PUBLIC_DIR, "live-sub.txt"), live_sub)
    for alt in ("live-sub.json", "live-sub.webp"):
        C.write_json(os.path.join(C.PUBLIC_DIR, alt), live_sub)
    # 富信息版（带note/kind），便于人看；App 只读上面的 urls
    C.write_json(os.path.join(C.PUBLIC_DIR, "subscriptions.detail.json"), {
        "updated_at": C.bjnow(), "count": len(profiles),
        "base_url": base_url(), "profiles": profiles,
        "live_profiles": live_tiers,
    })
    # 多仓订阅的常见后缀变体：影视仓等 App 习惯用 .txt/.webp 承载多仓订阅，
    # 填在「仓库 / 订阅地址」入口（不是「配置地址」）。内容仍是同一份 JSON。
    for alt in ("sub.txt", "sub.webp", "sub.json"):
        C.write_json(os.path.join(C.PUBLIC_DIR, alt), sub_obj)
    stats["profiles"] = len(profiles)
    stats["profiles_written"] = written_profiles
    stats["tiers_written"] = tiers_written
    stats["live_tiers"] = live_tiers
    stats["live_cleaned"] = {**live_clean_stats,
                             **{f"ref_{k}": v for k, v in ref_clean_stats.items()}}
    stats["subscription_url"] = f"{base_url()}/subscriptions.json"
    stats["sha256"] = {k: v[:16] for k, v in res.items()}
    return {"ok": len(sites) > 0, "stats": stats, "tv": tv, "hashes": res,
            "profiles": profiles}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats-only", action="store_true")
    ap.add_argument("--build", action="store_true", help="写入 public/")
    a = ap.parse_args()
    r = merge(build=not a.stats_only)
    C.log("[merge] " + json.dumps(r["stats"], ensure_ascii=False, indent=2))
    return 0 if (a.stats_only or r["ok"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())