"""
tv-hub 共用底座：配置加载 / HTTP 抓取 / 结构校验 / 缓存与状态存储 / 去重。

被 scripts/fetch.py、validate.py、merge.py、build.py 复用。
不引入任何第三方依赖（标准库 only），保证 GitHub Actions 开箱即跑。
"""
from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
CACHE_DIR = os.path.join(ROOT, "cache")
PUBLIC_DIR = os.path.join(ROOT, "public")
SOURCES_FILE = os.path.join(CONFIG_DIR, "sources.json")
STATE_FILE = os.path.join(CACHE_DIR, "_state.json")

VALID_TYPES = {"tvbox", "live_txt", "live_m3u", "subscription"}
TVBOX_LIST_FIELDS = ("sites", "parses", "lives", "flags", "doh", "rules")


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------
def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso(dt: _dt.datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bjnow() -> str:
    return utcnow().astimezone(_dt.timezone(_dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_sources(path: str | None = None) -> dict:
    # 运行时解析默认值，而非用默认参数绑定：
    # 默认参数会在函数定义时固化 SOURCES_FILE，导致测试中 patch 模块变量无效。
    path = path or SOURCES_FILE
    # utf-8-sig：同时兼容带 BOM 与不带 BOM 的配置（编辑工具可能加 BOM）
    with open(path, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict) or "sources" not in cfg:
        raise ValueError("sources.json 顶层结构错误：需要 {sources: [...]}")
    if not isinstance(cfg["sources"], list):
        raise ValueError("sources.json 的 sources 必须是数组")
    seen: set[str] = set()
    for i, s in enumerate(cfg["sources"]):
        for field in ("id", "name", "type", "primary"):
            if field not in s:
                raise ValueError(f"sources[{i}] 缺字段 {field}")
            if not str(s[field]).strip():
                raise ValueError(f"sources[{i}] 字段 {field} 为空")
        if s["id"] in seen:
            raise ValueError(f"重复的 source id: {s['id']}")
        seen.add(s["id"])
        if s["type"] not in VALID_TYPES:
            raise ValueError(f"{s['id']}: 未知 type={s['type']}，允许值 {sorted(VALID_TYPES)}")
        fb = s.get("fallback", [])
        if fb is not None and not isinstance(fb, list):
            raise ValueError(f"{s['id']}: fallback 必须是数组")
        if not str(s["primary"]).lower().startswith(("http://", "https://")):
            raise ValueError(f"{s['id']}: primary 必须是 http(s) 开头")
    return cfg


def enabled_sources(cfg: dict) -> list[dict]:
    return [s for s in cfg["sources"] if s.get("enabled", True)]


def defaults_of(cfg: dict) -> dict:
    d = cfg.get("defaults") or {}
    return {
        "timeout": int(d.get("timeout", 45)),
        "retries": int(d.get("retries", 2)),
        "retry_backoff_seconds": float(d.get("retry_backoff_seconds", 3)),
        "user_agent": d.get("user_agent", "tv-hub/1.0"),
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE  # 部分国内源证书链不全


class FetchError(Exception):
    def __init__(self, url: str, reason: str):
        super().__init__(reason)
        self.url = url
        self.reason = reason


def http_get(url: str, timeout: int = 45, retries: int = 2, user_agent: str = "tv-hub/1.0",
             backoff: float = 3.0, max_bytes: int = 64 * 1024 * 1024) -> tuple[bytes, int]:
    """返回 (body, http_status)。失败抛 FetchError（含明确原因）。"""
    last = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
                raw = resp.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise FetchError(url, f"响应体超过上限 {max_bytes}B")
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    try:
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                    except OSError:
                        pass
                status = int(getattr(resp, "status", 200) or 200)
                if status >= 400:
                    raise FetchError(url, f"HTTP {status}")
                return raw, status
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} {e.reason}"
            # 4xx（除 429）重试无意义
            if e.code != 429 and 400 <= e.code < 500:
                raise FetchError(url, last) from e
        except urllib.error.URLError as e:
            last = f"URLError: {getattr(e, 'reason', e)}"
        except FetchError as e:
            last = e.reason
            raise
        except Exception as e:  # noqa: BLE001 - 记录任意异常，保证不中断整体流程
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    raise FetchError(url, last or "unknown")


def fetch_first_ok(urls: list[str], **kw) -> tuple[bytes, int, str]:
    """按序尝试多个 URL，返回 (body, status, 实际成功使用的 URL)。全失败抛 FetchError。"""
    errs: list[str] = []
    for u in urls:
        if not u:
            continue
        try:
            body, st = http_get(u, **kw)
            return body, st, u
        except FetchError as e:
            errs.append(f"{u} -> {e.reason}")
    raise FetchError(urls[0] if urls else "(empty)", "; ".join(errs) or "无可用 URL")


# --------------------------------------------------------------------------
# 结构校验：TVBox 正确识别 sites / parses / flags / spider
# --------------------------------------------------------------------------
GROUP_MAP = {
    "央视频道": "央视", "卫视频道": "卫视", "央视": "央视", "卫视": "卫视",
    "浙江频道": "地方-浙江", "湖南频道": "地方-湖南", "江苏频道": "地方-江苏",
    "广东频道": "地方-广东", "上海频道": "地方-上海", "北京频道": "地方-北京",
    "山东频道": "地方-山东", "四川频道": "地方-四川", "河南频道": "地方-河南",
    "湖北频道": "地方-湖北", "安徽频道": "地方-安徽", "福建频道": "地方-福建",
    "江西频道": "地方-江西", "辽宁频道": "地方-辽宁", "黑龙江频道": "地方-黑龙江",
    "其他频道": "其他", "其他": "其他", "港·澳·台": "港台", "港澳台": "港台", "港台": "港台",
    "轮播": "轮播", "直播": "其他", "更新时间": "未分组", "🕘️": "未分组",
}
# 形如「XX频道」「XX台」→ 地方-XX
_REGION_SUFFIX = re.compile(r"^(.{2,3})(频道|电视台|台)$")


def norm_group(raw: str) -> str:
    """分组名归一化：去 emoji、去尾部逗号、映射同义组名。
    目的：让 txt 与 m3u 两路来源、以及不同上游的同名分组能合并成一组。"""
    g = (raw or "").strip().rstrip(",").strip()
    g = re.sub(r"^[^\w\u4e00-\u9fff]+", "", g).strip()
    if not g:
        return "未分组"
    if re.fullmatch(r"[\d\s:./\-年月日时分秒]+", g) or re.match(r"^\d{4}-\d{2}-\d{2}", g):
        return "未分组"
    if g in GROUP_MAP:
        return GROUP_MAP[g]
    if g.endswith("频道"):# 通用后缀：xx频道 -> 地方-xx（已映射的除外）
        base = g[:-2]
        if base not in ("央视频道", "卫视频道"):
            return f"地方-{base}"
    m = _REGION_SUFFIX.fullmatch(g)
    if m and m.group(1) not in GROUP_MAP:
        return f"地方-{m.group(1)}"
    if g.startswith("地方") and len(g) > 2:
        return g
    return g


def url_encodable(u: str) -> bool:
    """URL 是否能通过 latin-1 编码（Android java.net.URI / OkHttp 的前置要求）。

    实测（讴歌 6.0.9.3）：配置里只要有一个非 ASCII 域名（如中文域名），
    客户端解析 URL 时就会抛异常，导致整份配置加载失败或无限重试。
    Python 的 urlopen 用同一套 latin-1 规则，可准确复现。
    """
    if not isinstance(u, str) or not u:
        return False
    try:
        u.encode("latin-1")
        return True
    except UnicodeEncodeError:
        return False


# 同源相对路径：只允许干净的 ASCII 路径（可含 URL 编码 %xx）。
# 铁律：不允许 `..`（防目录逃逸）、不允许反斜杠、不允许空格。
_REL_PATH_RE = re.compile(r"^\./[A-Za-z0-9_\-./%\[\]()@+~,;=!$&']+$")

# 相对路径里需要保留原样的字符（URI 保留字 + 路径分隔符）。
_REL_SAFE = "/:@-_.~[]()!$&'+,;="


def norm_url(u: str) -> str:
    """把 URL 里的非 ASCII 部分转成 percent-encoding（能救则救，不能救原样返回）。

    为什么需要：上游大量 URL 的**路径**含中文而未编码，例如
      - `http://101.34.67.237/js/秋霞.js`      （201 个源的 ext 长这样）
      - `./deps/auto/24-243s/lib/EMO蓝光[V2].js`
    这些地址本身是有效的，只是没做 percent 编码。Android 的 `java.net.URI`
    拒绝非 ASCII，于是它们被清洗规则当「非法 URL」删掉 —— 源因此变成空壳。
    统一编码后，既保住这批源，又不引入真正不可解析的地址。

    边界（**不救**，返回原值交由 is_usable_url 判非法剔除）：
      - 非 ASCII 的 **host**（中文域名）。编码域名等于换了个域名，
        且部分客户端不做 punycode 转换，放过反而制造新的加载失败。
    """
    if not isinstance(u, str) or not u:
        return u
    try:
        u.encode("ascii")
        return u                      # 纯 ASCII，原样返回
    except UnicodeEncodeError:
        pass

    if u.startswith("./"):
        return quote(u, safe=_REL_SAFE)

    if not u.startswith(("http://", "https://")):
        return u
    try:
        sp = urlsplit(u)
    except ValueError:
        return u
    try:
        (sp.hostname or "").encode("ascii")
    except UnicodeEncodeError:
        return u                      # 中文域名：救不了
    path = quote(sp.path, safe="/:@-_.~[]()!$&'+,;=")
    query = quote(sp.query, safe="=&?/:@-_.~[]()!$&'+,;%")
    return urlunsplit((sp.scheme, sp.netloc, path, query, sp.fragment))


def norm_rel_path(p: str) -> str:
    """兼容旧调用名：只处理 `./` 相对路径（内部转调 norm_url）。"""
    return norm_url(p)


def is_usable_url(u: str) -> bool:
    """客户端能否使用这个地址。

    - **http(s) 绝对地址**：走 `is_valid_remote_url`（严格，中文域名等一律拒）
    - **`./` 开头的同源相对路径**：**允许**。

    为什么必须放行相对路径（2026-10-05 修正）：
      上游 hebijunge 整套体系都建立在相对路径上 —— 它的 `spider` 就是
      `./deps/feishu-sync/一木源/JAR/XB包jar/LIBVIO.jar;md5;...`，
      站点 ext 也大量写作 `./deps/.../xxx.js`。饭太硬的 spider 同样是
      `./fty.jar`。客户端会以「配置所在目录」为基准拼接这些地址，
      因此它们在同源部署下是可用的。

      此前把相对路径一律当「非法 URL」删除，导致 307 个源的 ext 被清空、
      变成点了没反应的空壳（用户误以为是「要会员」）。这是本项目的
      一个重要教训，已记入 LESSONS.md。

    安全约束：相对路径必须是纯 ASCII、URL 编码安全、且不含 `..`。
    """
    if not isinstance(u, str) or not u:
        return False
    if u.startswith("./"):
        if ".." in u or "\\" in u:
            return False
        return url_encodable(u) and bool(_REL_PATH_RE.match(u))
    return is_valid_remote_url(u)


def is_valid_remote_url(u: str) -> bool:
    """能否被客户端使用：http(s) + 非本地地址 + 可编码。"""
    if not url_encodable(u):
        return False
    if not u.startswith(("http://", "https://")):
        return False
    if u.startswith(("http://127.0.0.1", "http://localhost", "http://0.0.0.0")):
        return False
    return True


def scrub_urls(obj: Any, drop_keys: bool = True) -> Any:
    """递归剔除所有「客户端无法解析」的 URL 字符串。

    覆盖范围不止 api/ext/jar：实测上游还存在 key/homePage 等字段带中文域名
    （如 `http://iyiwang.com/花姐`、`.../厂长.html`），以及 query 里含中文
    （`?ou=33c` 实际是「没了」）。这些字段虽然客户端通常不请求，
    但保守起见一律清除，确保配置里不存在任何非 ASCII URL。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                v2 = norm_url(v)          # 先补 percent-encoding，再判非法
                if not is_valid_remote_url(v2):
                    if drop_keys:
                        continue      # 整个字段丢掉
                    out[k] = ""
                    continue
                out[k] = v2
                continue
            out[k] = scrub_urls(v, drop_keys)
        return out
    if isinstance(obj, list):
        res = []
        for v in obj:
            if isinstance(v, str):
                if v.startswith(("http://", "https://")):
                    v2 = norm_url(v)
                    if not is_valid_remote_url(v2):
                        continue
                    res.append(v2)
                    continue
                res.append(v)
                continue
            if isinstance(v, dict):
                cleaned = scrub_urls(v, drop_keys)
                # 清洗后变空壳的 dict 元素必须丢弃：
                #   - 全空
                #   - type=1 却没 api（api 被剔除）
                # 否则客户端 initSite 会遇到无法使用的站点而判定配置异常。
                # scrub_urls 只负责清URL，不做「是否该保留这个站点」的判断
                #（那是 merge.write_profiles / merge 主流程的职责）。
                # 若在此处丢站点，会造成站点数被静默削减。
                if isinstance(cleaned, dict) and not cleaned:
                    continue
                res.append(cleaned)
                continue
            res.append(scrub_urls(v, drop_keys))
        return res
    return obj


def detect_shape(data: Any) -> str:
    if isinstance(data, list):
        return "list"
    if isinstance(data, dict):
        if "urls" in data and isinstance(data["urls"], list) and "sites" not in data:
            return "subscription"
        if any(k in data for k in TVBOX_LIST_FIELDS):
            return "tvbox"
        return "dict"
    return type(data).__name__


def validate_tvbox(data: Any, min_sites: int = 0) -> dict:
    """返回 {ok, shape, counts, warnings, errors}。不同结构不会强行通过。
    min_sites=0 表示允许 sites 为空（直播壳类源合法，如TVBox-Sources 的 tvbox.json）。"""
    res: dict[str, Any] = {"ok": False, "shape": detect_shape(data), "counts": {},
                           "warnings": [], "errors": []}
    if res["shape"] != "tvbox":
        res["errors"].append(f"不是 TVBox 结构（shape={res['shape']}）")
        return res
    for f in TVBOX_LIST_FIELDS:
        if f in data:
            if not isinstance(data[f], list):
                res["errors"].append(f"字段 {f} 存在但不是数组（类型 {type(data[f]).__name__}）")
            else:
                res["counts"][f] = len(data[f])
    if "spider" in data and not isinstance(data["spider"], str):
        res["errors"].append("spider 字段必须是字符串")
    else:
        res["counts"]["spider"] = 1 if isinstance(data.get("spider"), str) and data["spider"] else 0
    for f in ("wallpaper", "logo", "warningText", "proxy", "sites_extra"):
        if isinstance(data.get(f), str) and data[f]:
            res["counts"].setdefault("scalars", 0)
            res["counts"]["scalars"] += 1
    bad = [i for i, s in enumerate(data.get("sites") or []) if not isinstance(s, dict)]
    if bad:
        res["errors"].append(f"sites 中有 {len(bad)} 个非对象元素（前几个 index: {bad[:5]}）")
    badp = [i for i, s in enumerate(data.get("parses") or []) if not isinstance(s, dict)]
    if badp:
        res["errors"].append(f"parses 中有 {len(badp)} 个非对象元素")
    n_sites = res["counts"].get("sites", 0)
    if n_sites < min_sites:
        res["errors"].append(f"sites 数量 {n_sites} < 最低要求 {min_sites}")
    if not res["errors"]:
        res["ok"] = True
    if n_sites == 0:
        res["warnings"].append("sites 为空（该源仅提供 lives/spider，不贡献站点）")
    return res


def validate_live_txt(text: str) -> dict:
    """`名称,URL` 每行；分组行实测有两种写法都要认：
         `央视,#genre#`   （上游实际用法，# 不在行首）
         `#genre#` / `#央视,#genre#`
    返回校验结果 + 频道列表。"""
    res: dict[str, Any] = {"ok": False, "counts": {"lines": 0, "channels": 0, "groups": 0},
                           "warnings": [], "errors": [], "channels": []}
    channels, cur = [], "未分组"
    seen_groups: set[str] = set()

    def set_group(label: str) -> None:
        nonlocal cur
        cur = norm_group(label)
        seen_groups.add(cur)

    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        res["counts"]["lines"] += 1
        # 分组行：包含 #genre# 即为分组（无论 # 在行首还是行尾）
        m_genre = re.search(r"#\s*genre\s*#", line, re.IGNORECASE)
        if m_genre:
            # 分组名= 第一个 # 之前的部分；行首写法（`#央视,#genre#`）需先剥掉前导 #
            label = line[: m_genre.start()]
            set_group(label.lstrip("#").strip())
            continue
        if line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 2:
            res["warnings"].append(f"跳过无 URL 行: {line[:60]}")
            continue
        url = parts[-1].strip()
        name = ",".join(parts[:-1]).strip() or url
        if not url.lower().startswith(("http://", "https://", "rtmp://", "rtsp://")):
            res["warnings"].append(f"跳过非法 URL: {url[:60]}")
            continue
        channels.append({"name": name, "url": url, "group": cur})
    res["counts"]["channels"] = len(channels)
    res["counts"]["groups"] = len(seen_groups)
    res["channels"] = channels
    if not channels:
        res["errors"].append("未解析出任何直播频道")
    else:
        res["ok"] = True
    return res


def validate_live_m3u(text: str) -> dict:
    """EXTINF + 下一行 URL；tvg-id 做身份。"""
    res: dict[str, Any] = {"ok": False, "counts": {"lines": 0, "channels": 0, "groups": 0},
                           "warnings": [], "errors": [], "channels": []}
    channels, cur = [], {"name": "", "group": "未分组", "tvg_id": "", "logo": ""}
    seen_groups: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        res["counts"]["lines"] += 1
        if line.startswith("#EXTM3U") or line.startswith("#EXTENC") or line.startswith("#PLAYLIST"):
            continue
        if line.startswith("#EXTINF"):
            m = re.search(r'group-title="([^"]*)"', line)
            if m:
                cur["group"] = norm_group(m.group(1))
                seen_groups.add(cur["group"])
            m = re.search(r'tvg-id="([^"]*)"', line)
            cur["tvg_id"] = m.group(1) if m else ""
            m = re.search(r'tvg-logo="([^"]*)"', line)
            cur["logo"] = m.group(1) if m else ""
            cur["name"] = line.rsplit(",", 1)[-1].strip()
            continue
        if line.startswith("#EXTGRP"):
            cur["group"] = norm_group(line.partition(":")[2])
            seen_groups.add(cur["group"])
            continue
        if line.startswith("#"):
            continue
        if not line.lower().startswith(("http://", "https://", "rtmp://", "rtsp://")):
            res["warnings"].append(f"跳过非法 URL: {line[:60]}")
            continue
        channels.append({"name": cur["name"] or line, "url": line,
                         "group": cur["group"], "tvg_id": cur["tvg_id"], "logo": cur["logo"]})
    res["counts"]["channels"] = len(channels)
    res["counts"]["groups"] = len(seen_groups)
    res["channels"] = channels
    if not channels:
        res["errors"].append("未解析出任何直播频道")
    else:
        res["ok"] = True
    return res


def validate_payload(src: dict, body: bytes) -> dict:
    """按 type 分派校验。body 为原始字节。"""
    kind = src["type"]
    if kind in ("live_txt", "live_m3u"):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("utf-8", errors="replace")
            return {"ok": False, "errors": ["非 UTF-8 编码"], "warnings": [], "counts": {}}
        return validate_live_m3u(text) if kind == "live_m3u" else validate_live_txt(text)
    try:
        data = json.loads(body.decode("utf-8-sig"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "shape": "invalid", "counts": {},
                "warnings": [], "errors": [f"JSON 解析失败: {type(e).__name__}: {e}"]}
    shape = detect_shape(data)
    if kind == "tvbox":
        return validate_tvbox(data, min_sites=0)
    if kind == "subscription":
        r = {"ok": False, "shape": shape, "counts": {"urls": 0},
             "warnings": [], "errors": []}
        if shape != "subscription":
            r["errors"].append(f"不是订阅结构（shape={shape}）")
            return r
        r["counts"]["urls"] = len(data["urls"])
        r["ok"] = True
        return r
    # 未显式声明类型的源：按结构自适应，但不强行合并
    if shape == "tvbox":
        return validate_tvbox(data, min_sites=0)
    return {"ok": False, "shape": shape, "counts": {}, "warnings": [],
            "errors": [f"不支持的 type={kind} 且结构无法识别（shape={shape}）"]}


def parse_payload(src: dict, body: bytes) -> Any:
    """校验通过后调用，把 body 转成可合并的对象。"""
    kind = src["type"]
    if kind in ("live_txt", "live_m3u"):
        return {"channels": validate_payload(src, body)["channels"]}
    return json.loads(body.decode("utf-8-sig"))


# --------------------------------------------------------------------------
# 缓存 & 状态
# --------------------------------------------------------------------------
def ensure_dirs() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(PUBLIC_DIR, exist_ok=True)


def cache_path(src_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{src_id}.json")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st, dict) and "sources" in st:
                st.setdefault("runs", [])
                return st
        except Exception:  # noqa: BLE001 - 状态文件损坏则重建
            pass
    return {"version": 1, "sources": {}, "runs": []}


def save_state(st: dict) -> None:
    ensure_dirs()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def sha256_of(data: bytes | str) -> str:
    b = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(b).hexdigest()


def write_json(path: str, obj: Any, bom: bool = False) -> str:
    """原子写。返回文件 sha256。

    默认不带 UTF-8 BOM：Android org.json/JSONObject 会把 BOM 当非法字符，
    导致整份配置解析失败（症状就是 App 一直提示「解析失败」）。
    bom=True 可显式开启供旧链路兼容（不再默认使用）。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig" if bom else "utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
    with open(path, "rb") as f:
        return sha256_of(f.read())


def log(*a: Any) -> None:
    print(*a, file=sys.stdout, flush=True)