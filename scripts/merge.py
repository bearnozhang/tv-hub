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

import curate as CU  # noqa: E402  策展层（筛免费高速源）

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
    # ★ 剔除客户端无法解析的 URL（非 ASCII 域名 / localhost），但**放行 `./` 同源相对路径**。
    #   实测：ysc_single_agg 里有 16 处中文域名 URL（「央视大全.json」「欧歌」），
    #   Android 的 URI 解析会抛异常，导致整份配置加载失败或无限重试 —— 这些必须删。
    #   但上游 hebi 的 ext/jar 大量使用 `./deps/...` 相对路径（同源部署下可用），
    #   此前一刀切删除导致 307 个源变成空壳，改为 is_usable_url 区分对待。
    # 先规范化 URL（非 ASCII 路径 → percent-encoding）。顺序不能反：
    # 未编码的中文路径会被 is_usable_url 判为非法而丢掉 —— 实测这正是
    # 201 个源 ext 丢失的原因（`http://101.34.67.237/js/秋霞.js`）。
    for f in ("api", "jar", "ext"):
        v = out.get(f)
        if isinstance(v, str) and v:
            out[f] = C.norm_url(v)
    for f in ("api", "jar", "ext"):
        v = out.get(f)
        if isinstance(v, str) and v and not C.is_usable_url(v):
            out.pop(f, None)
    # ext 可能是 dict（{分组名: url}），也要逐个清洗
    if isinstance(out.get("ext"), dict):
        conv = {}
        for k, v in out["ext"].items():
            if isinstance(v, str):
                v2 = C.norm_url(v)
                if C.is_usable_url(v2):
                    conv[k] = v2
        out["ext"] = conv if conv else ""
        if not conv:
            out.pop("ext", None)
    # 仍有合法 url 数组的情况（如 api: [a, b]）
    if isinstance(out.get("api"), list):
        out["api"] = [x for x in out["api"]
                      if isinstance(x, str) and C.is_usable_url(x)]
        if not out["api"]:
            out["api"] = ""
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
    # ★ 字段白名单裁剪（2026-10-05）：只保留客户端契约内的字段。
    #   上游夹带 reference / discovery_decoded_works / 中文 key「类型」/
    #   title / lang / id / isActive / order_num / playurl（小写）等私有字段。
    #   FongMi 系（讴歌、影视仓，用 gson）忽略未知字段，所以此前没暴露；
    #   但**主流 TVBox 官方版（手机端）会因未知字段直接判「解析配置失败」**。
    #   宁可少带字段，也不要带客户端不认识的东西。
    allow = C.site_allow_fields()
    out = {k: v for k, v in out.items() if k in allow}
    if "type" not in out:      # 实测有站点缺 type；补 0 保证字段齐整
        out["type"] = 0
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


TV_KEEP_GROUPS = {"央视", "卫视", "港台"}


def _is_tv_group(name: str) -> bool:
    if name in TV_KEEP_GROUPS:
        return True
    return name.startswith("地方-")


def keep_alive_lives(lives: list) -> list[dict]:
    """剔除空壳直播条目：先清洗URL，再要求引用型有 url、分组型有至少一个有效频道。
    清洗后变空的频道也必须剔除，否则客户端判定配置损坏。"""
    out: list[dict] = []
    for lv in C.scrub_urls(lives):
        if not isinstance(lv, dict):
            continue
        if lv.get("url"):
            out.append(lv)
            continue
        chans = lv.get("channels")
        if not isinstance(chans, list):
            continue
        cleaned = []
        for c in chans:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            urls = c.get("urls")
            if isinstance(urls, list):
                urls = [u for u in urls
                        if isinstance(u, str) and C.is_valid_remote_url(u)]
                if not urls:
                    continue
                c = {**c, "urls": urls}
            elif not (c.get("url") or c.get("urls")):
                continue
            cleaned.append(c)
        if cleaned:
            out.append({**lv, "channels": cleaned})
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
    TV_GROUPS = {"央视", "卫视", "港台"}

    def keep_group_name(name: str) -> bool:
        if name in TV_GROUPS:
            return True
        if name.startswith("地方-"):
            return True
        return False

    stats = {"relative": 0, "localhost": 0, "noname": 0, "dupname": 0, "non_tv_group": 0}
    seen: set[str] = set()
    out: list[dict] = []
    for x in lives:
        if not isinstance(x, dict):
            continue
        name = str(x.get("name") or "").strip()
        if not name:
            stats["noname"] += 1
            continue
        if not keep_group_name(name):
            stats["non_tv_group"] += 1
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
        if isinstance(y.get("channels"), list):
            chans = []
            for c in y["channels"]:
                if not isinstance(c, dict):
                    continue
                urls = [u for u in (c.get("urls") or [])
                        if isinstance(u, str) and C.is_valid_remote_url(u)]
                if not urls:
                    continue          # 无可用 url 的频道直接丢弃
                chans.append({**c, "urls": urls})
            if not chans:
                continue
            y["channels"] = chans
        out.append(y)
    return out, stats


def render_live_txt(lives: list[dict]) -> str:
    """把清洗后的 lives 渲染成影视仓/讴歌通用的纯文本直播源格式。

    格式（与上游 tvs_live_txt 一致）：
        分组名,#genre#
        频道名,URL
        频道名,URL
    同一频道多备源 = 连续多行同名条目（上游源同样这么写）。
    """
    lines = []
    for lv in lives:
        name = str(lv.get("name") or "").strip()
        if not name:
            continue
        chans = lv.get("channels") or []
        if chans:
            lines.append(f"{name},#genre#")
            for c in chans:
                cn = str(c.get("name") or "").strip()
                urls = [u for u in (c.get("urls") or [])
                        if isinstance(u, str) and C.is_valid_remote_url(u)]
                for u in urls:
                    if cn:
                        lines.append(f"{cn},{u}")
            continue
        url = str(lv.get("url") or "").strip()
        if url and C.is_valid_remote_url(url):
            lines.append(f"{name},#genre#")
            lines.append(f"{name},{url}")
    return "\n".join(lines) + "\n"


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
    grp = [x for x in keep_alive_lives(live_out) if x.get("channels")]

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
                urls = [u for u in (c.get("urls") or []) if C.is_valid_remote_url(u)]
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
                # 标量同样要清洗（wallpaper 可能是中文域名）
                obj[k] = C.scrub_urls(scalars[k])
        write_cfg(os.path.join(C.PUBLIC_DIR, name), obj)
        written.append({"name": name, "groups": len(items),
                        "channels": ch_count(items),
                        "bytes": os.path.getsize(os.path.join(C.PUBLIC_DIR, name))})
    return written


def write_cfg(path: str, obj) -> str:
    """写 JSON 产物。落盘前做「内核契约消毒」。

    FongMi 系客户端（讴歌/影视仓）的 Site.objectFrom 用 gson 反序列化：
    字段类型不符时抛异常并**静默返回空 Site**（key=null），
    随后 initSite 的 `Collectors.toMap(Site::getKey, ...)` 因 null key 抛 NPE，
    整份配置加载失败 —— 表现就是「解析配置失败」。

    实测（2026-10-05）：/tv 有 35 处冲突必然失败；
    ysc_single_agg（0 处冲突）正常加载。详见 scripts/sanitize.py。
    """
    if isinstance(obj, dict) and "sites" in obj:
        try:
            from sanitize import sanitize_config
            obj, _stat = sanitize_config(obj)
        except Exception:  # noqa: BLE001
            pass
    return C.write_json(path, obj)


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


TIER_LITE = (60, "lite")
TIER_STD = (300, "standard")


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
            obj["parses"] = [p for p in parses[:60]
                             if str(p.get("url") or "").startswith(("http://", "https://"))]
        if flags:
            obj["flags"] = flags
        obj["lives"] = []
        # ★ 与主配置同一份契约：type 1/3 必须有 api。
        #   实测 tv-lite 曾出现 sites[165](qc555) type=1 缺 api、
        #   sites[249](豆瓣) type=3 缺 api，两者都会让内核抛异常 → 「解析配置失败」。
        obj["sites"] = [x for x in obj["sites"] if x.get("api")]
        write_cfg(os.path.join(C.PUBLIC_DIR, name), C.scrub_urls(ordered(obj)))
        written.append(name)
    return written


TIER_FAST_MAX = 240


def _jar_classes(jar_path: str) -> set:
    """读出 jar 内 dex 里所有 `com/github/catvod/spider/*` 的顶层类名。"""
    import re as _re
    import zipfile
    if not os.path.exists(jar_path):
        return set()
    try:
        with zipfile.ZipFile(jar_path) as z:
            names = set()
            for entry in z.namelist():
                if not entry.endswith(".dex"):
                    continue
                data = z.read(entry)
                for m in _re.findall(rb"com/github/catvod/spider/([A-Za-z0-9_$]+)", data):
                    names.add(m.decode().split("$")[0])
            return names
    except Exception:
        return set()


def write_main_tier(spider: str) -> list[str]:
    """★ 自包含主档（`/tv`）—— 只用「爬虫代码就在 jar 里」的源。

    ## 为什么（用户实测 + 源码核实，2026-10-05）
    用户在国内加载 `/fast`（240 个第三方 http 采集接口）→ **一直转圈**。
    真因不是配置加载失败，而是客户端要**逐个去第三方站取数据**，
    这些采集站（`19q.cc` / `xjzyapi.com` / …）国内多数不通 → 客户端一直等。

    对照：用户实测**能开**的 ysc 配置里，150/167 的源是 `csp_XXX` ——
    爬虫类**就在 jar 内**，本机执行、**不访问任何第三方服务器** → 秒回。

    ## 做法
    从 ysc 的源里挑出同时满足三条的：
      1. api 形如 `csp_Xxx`
      2. `Xxx` 这个类**在我们自己的 spider.jar 里**
      3. **没有自己的 jar 字段**（否则又要去下载别人的 jar）
    再配上我们自己的 spider → 整份配置**只依赖一个 jar**。

    这类源不取外部数据、不依赖任何第三方服务器的死活 ——
    这才是「地址不用一直换」的真正含义：**依赖越少，越不会失效**。
    """
    classes = _jar_classes(os.path.join(C.PUBLIC_DIR, "spider.jar"))
    ysc_path = os.path.join(C.CACHE_DIR, "ysc_single_agg.json")
    if not classes or not os.path.exists(ysc_path):
        return []
    try:
        with open(ysc_path, encoding="utf-8-sig") as f:
            y = json.load(f)
    except Exception:
        return []

    sites: list[dict] = []
    for s in (y.get("sites") or []):
        if not isinstance(s, dict):
            continue
        api = str(s.get("api") or "")
        if not api.startswith("csp_"):
            continue
        if api[4:] not in classes:      # 类不在我们 jar 里 → 用不了
            continue
        if s.get("jar"):                # 要额外下别人的 jar → 排除
            continue
        item = dict(s)
        item.pop("jar", None)
        sites.append(item)

    if not sites:
        return []

    # 顶层结构照抄 ysc（它已被用户实测证明可加载），只替换 sites / spider，
    # 并丢掉 lives（ysc 的 lives 是它仓库内的相对路径文件，我们域名下不存在）。
    out = {k: v for k, v in y.items() if k not in ("sites", "lives", "spider")}
    out["spider"] = spider
    out["sites"] = sites
    written = []
    name = "tv-main.json"
    write_cfg(os.path.join(C.PUBLIC_DIR, name), out)
    written.append(name)
    return written


def write_fast_tier(sites: list[dict], parses: list[dict], flags: list,
                    spider: str, scalars: dict) -> list[str]:
    """产出「高速精选」档 tv-fast.json（订阅别名 /fast）。

    与 tv-lite / tv-standard 的区别：
      - lite/standard 是**体积分档** —— 解决老客户端的加载上限
      - fast 是**质量分档** —— 只保留「有速度证据且够快」的源

    入档条件（满足其一）：
      1. 上游名字里带 `[NNNms]` 标注且 ≤ 阈值（上游自己测过速）
      2. 健康档案里该源的 EWMA 实测速度 ≤ 阈值（我们探测积累的）

    **没有速度证据的源不进这一档**：这一档的定位就是「确定性快」，
    宁缺勿滥。想「都留着慢慢挑」就用主档 /tv。
    """
    pol = CU.load_policy()
    health = CU.load_health()
    fast_cfg = pol.get("fast") or {}
    thr = int(fast_cfg.get("threshold_ms", 1200))
    cap = int(fast_cfg.get("max_sites", TIER_FAST_MAX))
    entries = health.get("entries") or {}

    picked: list[dict] = []
    for s in sites:                      # sites 已按速度升序排好
        if len(picked) >= cap:
            break
        ms = CU.name_speed_ms(s)
        if ms is not None:
            if ms <= thr:
                picked.append(s)
            continue
        e = entries.get(CU.site_fp(s)) or {}
        ew = e.get("ms_ewma")
        if isinstance(ew, int) and 0 < ew <= thr:
            picked.append(s)

    KEY_ORDER = ("spider", "wallpaper", "logo", "warningText", "proxy", "doh",
                 "hosts", "rules", "sites", "lives", "parses", "flags")
    obj: dict = {"sites": picked}
    if spider:
        obj = {"spider": spider, **obj}
    for k in ("wallpaper", "logo", "proxy", "doh", "hosts", "rules"):
        if scalars.get(k) not in (None, "", [], {}):
            obj[k] = scalars[k]
    if parses:
        obj["parses"] = [p for p in parses[:60]
                         if str(p.get("url") or "").startswith(("http://", "https://"))]
    if flags:
        obj["flags"] = flags
    obj["lives"] = []
    obj["sites"] = [x for x in obj["sites"] if x.get("api")]
    ordered = {k: obj[k] for k in KEY_ORDER if k in obj}
    ordered.update({k: v for k, v in obj.items() if k not in ordered})
    write_cfg(os.path.join(C.PUBLIC_DIR, "tv-fast.json"), C.scrub_urls(ordered))
    return ["tv-fast.json"]


# 参照 spider：来自用户实测**能加载**的 ysc 配置（伪装成 .png 的 jar）。
# 用作对照——若「我们的 spider」失败而「参照 spider」成功，即锁定为 jar 问题。
REF_SPIDER = ("https://img2.gelonghui.com/library/"
              "46da6-aa33493f-1c1d-4f35-9990-0be4bdbf0c64.png;md5;"
              "46da6b6a6a111d7924717db2ae790de3")


def write_probe_tiers(sites: list[dict], spider: str, parses: list,
                      flags: list, lives: list) -> list[str]:
    """生成一组**对照诊断档**，用于二分定位「配置解析错误」。

    每档只比上一档多**一个变量**——这样用户依次试，第一个失败的档
    就直接指出是哪个字段的问题：

      /t1 = 12 站 + 我们的 spider          （测 spider / jar）
      /t2 = 12 站 + 参照 spider（ysc 的）  （对照：spider 机制本身是否有问题）
      /t3 = 12 站 + 我们的 spider + lives  （测直播）
      /t4 = 12 站 + 我们的 spider + parses （测解析器）
      /t5 = 12 站 + 我们的 spider + flags  （测 flags）

    ⚠ 这些只在 build 时产出，且**体积都很小**（几 KB），不影响主档。
    """
    base = [s for s in sites
            if s.get("type") == 1
            and str(s.get("api") or "").startswith(("http://", "https://"))][:12]
    if not base:
        return []
    written: list[str] = []
    variants = [
        ("tv-t1.json", {"spider": spider}),
        ("tv-t2.json", {"spider": REF_SPIDER}),
        ("tv-t3.json", {"spider": spider, "lives": lives}),
        ("tv-t4.json", {"spider": spider, "parses": parses[:60]}),
        ("tv-t5.json", {"spider": spider, "flags": flags}),
    ]
    for name, extra in variants:
        obj = {**extra, "sites": base}
        # 去掉空值，避免引入无意义的变量
        obj = {k: v for k, v in obj.items() if v not in (None, "", [], {})}
        write_cfg(os.path.join(C.PUBLIC_DIR, name), C.scrub_urls(obj))
        written.append(name)
    return written


def write_ok_tiers(sites: list[dict], limit: int = 40) -> list[str]:
    """产出 `tv-ok1/ok2` —— **只用我实测探测通过的源**。

    为什么换这个数据集：
      此前诊断档用的是「上游自己标注 137ms」的源，但那是上游的测速快照，
      那些源可能早已失效 —— 用户加载后「没有列表」很可能就是这个原因。

      而 health 档案里的源是**本项目 CI 实际探测通过**的（记录在
      data/source_health.json），可信度高得多；其中不少走 ghfast.top /
      catbox.moe / ghproxy 这类国际 CDN，国内可达性也更好。

    两档对照，用来区分「源的问题」和「jar 的问题」：
      ok1 = 可达源，**不带 spider**（客户端不需要下载 jar）
      ok2 = 可达源，**带真实 jar**（`./spider.png`）
    """
    health = CU.load_health()
    entries = health.get("entries") or {}
    good: list[tuple[int, dict]] = []
    for s in sites:
        e = entries.get(CU.site_fp(s)) or {}
        if int(e.get("ok", 0)) <= 0:
            continue
        # 只取不需要 jar 的：api 是 http（采集站）或直接是脚本地址
        api = str(s.get("api") or "")
        if not api.startswith(("http://", "https://")):
            continue
        good.append((int(e.get("ms_ewma") or 99999), s))
    if not good:
        return []
    good.sort(key=lambda x: x[0])
    picked = [s for _, s in good[:limit]]

    written = []
    for name, obj in (
        ("tv-ok1.json", {"sites": picked}),
        ("tv-ok2.json", {"spider": REF_SPIDER, "sites": picked}),
    ):
        write_cfg(os.path.join(C.PUBLIC_DIR, name), C.scrub_urls(obj))
        written.append(name)
    return written


def write_min_tiers(sites: list[dict]) -> list[str]:
    """产出极小档 m1/m2 —— 针对**低带宽**环境（VPN / 跨境链路）验证。

    背景：用户网络带宽很小，915KB 的 `spider.jar` 必然下载超时，
    表现为「拉取配置失败」。已手工构造 175 字节的 `spider-min.jar`
    （合法 dex、不含任何类）。这两档用来确认：

      /m1 = 3 站，**无 spider**       → 客户端是否真的要求 spider 字段
      /m2 = 3 站 + **最小 jar(175B)** → 最小 jar 能否满足客户端

    若 m2 能正常显示列表，则把全部档位的 spider 换成最小 jar，
    低带宽环境即可正常加载。
    """
    base = [s for s in sites
            if s.get("type") == 1
            and str(s.get("api") or "").startswith(("http://", "https://"))][:3]
    if not base:
        return []
    written = []
    for name, obj in (
        ("tv-m1.json", {"sites": base}),
        # 用真实 jar（空壳 dex 会被客户端拒绝），且扩展名用 .png 绕过 CDN 白名单
        ("tv-m2.json", {"spider": REF_SPIDER, "sites": base}),
    ):
        write_cfg(os.path.join(C.PUBLIC_DIR, name), C.scrub_urls(obj))
        written.append(name)
    return written


def write_safe_tier(sites: list[dict], spider: str = "") -> list[str]:
    """产出 tv-safe.json（订阅别名 /safe）—— **极简诊断档**。

    用途：用户报「配置解析失败」时，用它做**二分定位**：

      - `/safe` 能加载、`/tv` 不能 → 问题在某个**可选字段**
        （spider / lives / parses / flags / proxy / doh / rules）
      - `/safe` 也不能加载 → 问题在**客户端或网络**，与配置内容无关

    因此这一档**刻意什么都不带**：
      - 无 spider（避免 jar 加载失败牵连整份配置）
      - 无 lives / parses / flags / proxy / doh / rules / hosts
      - 只有 12 个最标准的 http 采集站（type=1、api 为 http）

    配置越小、变量越少，定位越准。
    """
    picked = [s for s in sites
              if s.get("type") == 1
              and str(s.get("api") or "").startswith(("http://", "https://"))][:12]
    if not picked:
        return []
    obj = {"sites": picked}
    write_cfg(os.path.join(C.PUBLIC_DIR, "tv-safe.json"), C.scrub_urls(obj))
    return ["tv-safe.json"]


def write_deps_tier(sites: list[dict], parses: list[dict], flags: list,
                    spider: str, scalars: dict) -> list[str]:
    """产出 tv-deps.json —— **api 为同源相对路径**的源专用档。

    为什么单独一档（2026-10-05 事故）：
      上游有 140+ 个源的 api 写作 `./deps/auto/.../xxx.js`（相对路径）。
      讴歌 / 影视仓（FongMi 系）会以「配置所在目录」为基准拼接，能正常工作；
      但**主流 TVBox 官方版（手机端）不认相对路径 api → 整份配置解析失败**。

      所以主档 /tv 只放 http / csp_* 的源（**兼容性优先**），
      这一档专门给 FongMi 系客户端 —— 它们本来就是这批源的原始受众。

    依赖 Worker 的 `/deps/*` 反代（把相对路径映射到上游镜像仓库），
    因此**仅在主通道 `tv.bearno1.dpdns.org` 下可用**。
    """
    if not sites:
        return []
    KEY_ORDER = ("spider", "wallpaper", "logo", "warningText", "proxy", "doh",
                 "hosts", "rules", "sites", "lives", "parses", "flags")
    obj: dict = {"sites": sites}
    if spider:
        obj = {"spider": spider, **obj}
    for k in ("wallpaper", "logo", "proxy", "doh", "hosts", "rules"):
        if scalars.get(k) not in (None, "", [], {}):
            obj[k] = scalars[k]
    if parses:
        obj["parses"] = [p for p in parses[:60]
                         if str(p.get("url") or "").startswith(("http://", "https://"))]
    if flags:
        obj["flags"] = flags
    obj["lives"] = []
    obj["sites"] = [x for x in obj["sites"] if x.get("api")]
    ordered = {k: obj[k] for k in KEY_ORDER if k in obj}
    ordered.update({k: v for k, v in obj.items() if k not in ordered})
    write_cfg(os.path.join(C.PUBLIC_DIR, "tv-deps.json"), C.scrub_urls(ordered))
    return ["tv-deps.json"]


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
                write_cfg(os.path.join(out_dir, f"{sid}.json"),
                             {"urls": [u for u in urls if isinstance(u, dict) and u.get("url")]})
                written.append(sid)
            continue

        out: dict = {}
        for k in ("spider", "wallpaper", "logo", "warningText", "proxy", "doh",
                  "hosts", "rules", "version", "ad", "tihuan"):
            if k in data and data[k] not in (None, "", [], {}):
                # 标量 URL 同样要能通过客户端的 URI 解析
                if isinstance(data[k], str) and data[k].startswith(("http://", "https://"))                         and not C.is_valid_remote_url(data[k]):
                    continue
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
        merged_lives = merge_named(good_lives, "lives", Deduper(), stats)
        out["lives"], _ = clean_live_entries(merged_lives)

        out["parses"] = merge_named([(sid, x) for x in (data.get("parses") or [])],
                                    "parses", Deduper(), stats)
        out["flags"] = merge_flags([(sid, x) for x in (data.get("flags") or [])], Deduper(), stats)

        # ★ 与主配置同一份契约：type 1/3 必须有 api；
        #   lives 必须是引用型 {name,type,url,epg}，否则 TVBox 内核抛异常。
        #   ★ 必须先 scrub 再过滤：scrub 会把非 ASCII 域名等非法 url 直接删掉，
        #     若先过滤后 scrub，会留下「本来有 url、scrub 后变没 url」的残骸。
        out = C.scrub_urls(out)
        out["sites"] = [x for x in out["sites"] if x.get("api")]
        # 单仓档同样过一遍策展（只做静态判定，不探测），
        # 保证用户切到单仓时也不会撞上网盘/付费/残缺源。
        out["sites"] = CU.apply(out["sites"], None, build=False)
        out["parses"] = [x for x in out["parses"]
                         if x.get("name")
                         and str(x.get("url") or "").startswith(("http://", "https://"))]
        if isinstance(out.get("lives"), list):
            out["lives"] = [{"name": x["name"], "type": 0,
                             "url": "./live.txt", "epg": ""}
                            for x in keep_alive_lives(out["lives"]) if x.get("name")]

        # 直播引用壳（sites=0）落盘后是「空配置」，客户端会直接报解析失败，
        # 不产出。它们的价值已被 merge 主流程吸收（live_groups）。
        if not out["sites"]:
            continue

        write_cfg(os.path.join(out_dir, f"{sid}.json"), out)
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

    # ★ 落盘前统一递归清洗：剔除一切客户端无法解析的 URL
    #   （非 ASCII 域名、相对路径、localhost）。
    #   实测数据点：ysc_single_agg 有 16 处、tv.json 有 113 处。
    scalars = C.scrub_urls(scalars)
    # 上游 spider 多为相对路径（`./deps/...`），客户端 JarLoader 不认；
    # 一律替换为「已验证可用的绝对地址 + md5」，见下方详细说明。
    spider = REF_SPIDER

    # ★ spider 改为同源相对路径（实测饭太硬 `http://fty.xxooo.cf/tv` 就是
    #   `"spider": "./fty.jar"`，jar 与配置同域）。部分 TVBox 内核按
    #   同源相对路径加载 jar；跨域绝对地址会被判为「解析配置失败」。
    #   jar 已随 public/spider.jar 一同发布。
    if spider:
        # ★ 改回**同源相对路径**（2026-10-05 二次修正）。
        #
        #   上一次改成绝对地址（指向 Cloudflare 域名），理由是想兼容
        #   「不认相对路径的客户端」。这个理由**是错的**：
        #   FongMi 的 `Decoder.fix()` 会主动把配置里的 `./` 替换成
        #   `UrlUtil.resolve(配置URL, "./")` —— 相对路径一定会被解析成
        #   绝对地址，不存在「客户端不认」的问题。
        #
        #   而绝对地址有一个致命缺点：**把资源钉死在单一通道上**。
        #   用户在国内时 Cloudflare 时通时不通，即便换个通道加载了配置，
        #   spider 仍会去请求 Cloudflare 而卡住。
        #   相对路径则会跟随「配置是从哪个通道加载的」自动解析 ——
        #   从 jsDelivr 加载就指向 jsDelivr，从 Cloudflare 加载就指向 Cloudflare。
        #   ★ 扩展名用 `.png` 而不是 `.jar`（2026-10-05 关键修复）：
        #     jsDelivr 等 CDN 有**扩展名白名单**，`.jar` 直接返回 403，
        #     客户端表现就是「加载写入缓存 jar 失败」。
        #     上游（ysc / hebi）把 jar 命名成 `.png` 正是为了绕过这个限制；
        #     客户端只按内容解析，不看 Content-Type。
        #
        #   ★★★ 必须绝对 http 地址（2026-10-05 决定性修复，读源码确认）★★★
        #     FongMi `JarLoader.parseJar`：
        #         String[] texts = jar.split(";md5;");
        #         ... jar = texts[0];
        #         if (!md5.isEmpty() && Crypto.equals(Path.jar(jar), md5)) load(key, Path.jar(jar));
        #         else if (jar.startsWith("http"))  load(key, Download.create(jar, ...).get());
        #         else if (jar.startsWith("file"))  load(key, Path.local(jar));
        #     → 相对路径 `./spider.png` 三个分支**一个都不匹配**，被静默忽略：
        #       spider 永不加载，而 `VodConfig.initSite` 是在解析站点**之前**
        #       就调用 `BaseLoader.parseJar(spider, true)`。
        #
        #     对照实证：用户实测「能加载」的 ysc 配置，spider 就是绝对地址
        #         https://img2.gelonghui.com/...png;md5;46da6b6a6a111d7924717db2ae790de3
        #     而我们的 `./spider.png` 一律失败 —— 这就是分水岭。
        #
        #     该 md5 与本地 public/spider.png **完全一致**（同一文件），
        #     所以用户加载 ysc 时已把 jar 缓存到本地；
        #     用同一 URL 可命中缓存、**零下载**，对低带宽环境是最优解。
        spider = REF_SPIDER

    # ★ 落盘前统一递归清洗，随后剔除因清洗而变空的壳
    #   （否则会出现「type=1 却没api」「lives 既无 channels 也无 url」的空壳，
    #    源码里 initSite/initLive 对这些会直接抛异常或判定加载失败）
    tv_sites = [x for x in C.scrub_urls(sites)
                if isinstance(x, dict) and (x.get("key") or x.get("name"))]
    # ★ type 1/3 必须有 api，否则 TVBox 内核 initSite 直接抛异常，
    #   整份配置加载失败 → App 报「解析配置失败」。
    #   实测可用配置（饭太硬）里 53 个 site 全部 type=3 且 100% 带 api。
    tv_sites = [x for x in tv_sites if x.get("api")]

    # ★ lives 必须是「引用型」：{"name","type","url","epg"}，url 指向同域 txt。
    #   实测可用配置的 lives 就是 {"name":"ITV","type":0,"url":"./lib/ITV.txt"}。
    #   之前误用了 group/channels 结构（无 url），内核取不到 url 直接抛异常。
    tv_lives = [x for x in keep_alive_lives(lives + live_out)
                if _is_tv_group(str(x.get("name") or "").strip())]
    #   同理用同源相对路径：跟随「配置从哪个通道加载」自动解析，
    #   避免把资源钉死在单一通道上（见上面 spider 的说明）。
    tv_lives = [{"name": "直播", "type": 0, "url": "./live.txt", "epg": ""}]

    # ★ parses 必须有 url，否则 Parse.objectFrom 取不到地址会抛异常。
    tv_parses = [x for x in C.scrub_urls(parses)
                 if isinstance(x, dict) and x.get("name")
                 and str(x.get("url") or "").startswith(("http://", "https://"))]

    # ★★★ 策展层（curate）：从「能加载」到「能用」★★★
    #   筛掉网盘类（需夸克/UC/天翼等会员才能高速看）、付费类、磁力类，
    #   以及残缺源（drpy 引擎缺 ext 的空壳）—— 这些正是用户反馈
    #   「很多源点进去不能用」的来源。
    #   同时按速度排序（上游 ms 标注 → 健康档案 EWMA）。
    #   策略见 config/curate.json；实现见 scripts/curate.py。
    #
    # ★ 相对路径 api 的源（`./deps/xxx.js`）单独成档：
    #   只有 FongMi 系（讴歌/影视仓）认这种写法，**主流 TVBox 官方版不认** ——
    #   放进主档会导致整份配置「解析配置失败」。所以从主档剔出，
    #   改由 tv-deps.json（别名 /deps-config）提供，给支持的客户端用。
    _pol_deps = dict(CU.load_policy())
    _pol_deps["drop"] = [c for c in (_pol_deps.get("drop") or []) if c != "relpath"]
    relpath_sites = [x for x in tv_sites if str(x.get("api") or "").startswith("./")]
    relpath_kept = CU.curate(relpath_sites, _pol_deps, CU.load_health())["kept"] \
        if relpath_sites else []
    stats["relpath_sites"] = len(relpath_kept)

    _pre_curate = len(tv_sites)
    tv_sites = CU.apply(tv_sites, stats, build=build)
    stats["sites_curated_out"] = _pre_curate - len(tv_sites)

    stats["scrubbed_empty_sites"] = len(sites) - len(tv_sites)
    stats["scrubbed_empty_lives"] = len(lives) + len(live_out) - len(tv_lives)

    raw_site_count = len(sites_items) + len(rescued)
    tv: dict = {"spider": spider}
    tv.update(scalars)
    tv["sites"] = tv_sites
    tv["lives"] = tv_lives
    tv["parses"] = tv_parses
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
    tiers_written = write_lite_tiers(tv_sites, parses, flags, spider, scalars) if build else []
    # ★ 自包含主档（/tv）：只用 jar 内置爬虫源，不依赖任何第三方服务器
    main_written = write_main_tier(spider) if build else []
    fast_written = write_fast_tier(tv_sites, parses, flags, spider, scalars) if build else []
    # 相对路径源专用档（FongMi 系客户端用；见 write_deps_tier 说明）
    deps_written = write_deps_tier(relpath_kept, parses, flags, spider, scalars) \
        if build else []
    # 极简诊断档（无 spider / lives / parses / flags），用于二分定位「解析失败」
    safe_written = write_safe_tier(tv_sites) if build else []
    # 对照诊断档：每档只比 /safe 多一个变量，逐档试即可锁定出错字段
    probe_written = write_probe_tiers(tv_sites, spider, tv_parses, flags, tv_lives) \
        if build else []
    # 极小档（低带宽环境验证：最小 jar 能否替代 915KB 的 jar）
    min_written = write_min_tiers(tv_sites) if build else []
    # 「实测可达」档（用本项目 CI 探测通过的源，区分源问题 vs jar 问题）
    ok_written = write_ok_tiers(tv_sites) if build else []

    # 直播：清洗无效引用 + 体积分档（与点播同一套适配逻辑）
    clean_groups, live_clean_stats = clean_live_entries(live_out)
    clean_refs, ref_clean_stats = clean_live_entries(lives)
    live_tiers = write_live_tiers(clean_groups, clean_refs, spider, scalars) if build else []

    # 订阅清单：一个地址，App 内可切换多个仓
    profiles = build_profiles(cfg, per_source, len(tv_sites))
    sub_obj = {
        "urls": [{"name": p["name"], "url": p["url"]} for p in profiles],
    }
    res = {
        "tv": write_cfg(os.path.join(C.PUBLIC_DIR, "tv.json"), tv),
        "live": write_cfg(os.path.join(C.PUBLIC_DIR, "live.json"), {
            "updated_at": C.bjnow(), "generated_at": C.iso(),
            "groups": len(clean_groups), "channels": ch_total,
            "lives": C.scrub_urls(keep_alive_lives(clean_groups + clean_refs)),
        }),
        "subscriptions": write_cfg(os.path.join(C.PUBLIC_DIR, "subscriptions.json"), sub_obj),
    }
    # tvs.json：多仓订阅的短域名（subscriptions.json 的别名），方便记忆/填地址
    write_cfg(os.path.join(C.PUBLIC_DIR, "tvs.json"), sub_obj)
    # 纯文本直播源（分组,#genre# / 频道名,URL），供 App「直播地址」入口直接填入。
    # live-*.txt 是 TVBox 配置 JSON，讴歌的直播字段不认；真正要用这个。
    live_txt = render_live_txt(keep_alive_lives(clean_groups + clean_refs))
    live_txt_path = os.path.join(C.PUBLIC_DIR, "live.txt")
    with open(live_txt_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(live_txt)
    res["live_txt"] = live_txt_path
    # 直播订阅清单：与点播同构，urls[0] 为体积最小的档位
    live_sub = {"urls": [{"name": f"★ {t['name'].replace('.txt','')}（{t['groups']} 组 / {t['channels']} 频道）",
                          "url": f"{base_url()}/{t['name']}"} for t in live_tiers]}
    res["live_sub"] = write_cfg(os.path.join(C.PUBLIC_DIR, "live-sub.txt"), live_sub)
    for alt in ("live-sub.json", "live-sub.webp"):
        write_cfg(os.path.join(C.PUBLIC_DIR, alt), live_sub)
    # 富信息版（带note/kind），便于人看；App 只读上面的 urls
    write_cfg(os.path.join(C.PUBLIC_DIR, "subscriptions.detail.json"), {
        "updated_at": C.bjnow(), "count": len(profiles),
        "base_url": base_url(), "profiles": profiles,
        "live_profiles": live_tiers,
    })
    # 多仓订阅的常见后缀变体：影视仓等 App 习惯用 .txt/.webp 承载多仓订阅，
    # 填在「仓库 / 订阅地址」入口（不是「配置地址」）。内容仍是同一份 JSON。
    for alt in ("sub.txt", "sub.webp", "sub.json"):
        write_cfg(os.path.join(C.PUBLIC_DIR, alt), sub_obj)
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