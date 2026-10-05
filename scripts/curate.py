#!/usr/bin/env python3
"""curate.py —— 源策展器：把「能加载」变成「能用」。

## 为什么需要这一层

merge.py 负责「聚合并去重」，产出的站点**格式合法、能被客户端加载**。
但「能加载」不等于「能用」。用户实测反馈集中在两点：

  1. 大量源是**网盘类**（夸克 / UC / 阿里 / 天翼 / 115 / 百度云盘搜索、
     盘搜聚合）。没有对应网盘会员时，点进去要么报错、要么被限速到
     几十 KB/s，根本看不了。
  2. 一部分源是**付费 / 会员专属** —— 数据源自身要求充值或授权 cookie。

用户要的是「免费且高速」。因此在落盘之前必须再加一层：
按「是否需要付费 / 是否依赖网盘会员」筛选，并按实测速度排序。

## 三层判定

  L1 静态分类（可靠，不依赖网络）
     - pan     网盘类：api / ext 命中网盘搜索技术特征
     - paid    付费类：高置信付费特征（puqu、会员专区等）
     - magnet  磁力类：P2P 依赖种子，速度不可控
     - js      type=3 的 drpy JS 源（多数优质，保留）
     - direct  type=0/1 的 http 采集接口（主力）
     - invalid 缺 api 或 api 非法（空壳，必剔除）

  L2 主动探测（可选，--probe / policy.speed.probe_enabled）
     对 direct 类的 api 发 `?ac=list`，一次请求同时回答三件事：
     「活着没 / 多快 / 要不要钱」。

  L3 健康档案（自我优化，data/source_health.json）
     持久化每个源的历史表现：EWMA 速度、连续失败次数、首次/末次见到。
     连续 N 轮探测失败 → 隔离（quarantine）；任何一轮成功 → 立即恢复。
     档案随仓库提交，因此**每一轮运行都站在上一轮的结果上** —— 这就是
     「自我优化」的落点：不是玄学自学习，而是数据驱动的持续演化。

## 防误伤（关键设计）

探测在 CI 里跑（GitHub 机房），网络视角与中国大陆用户不同：
有些源对中国 IP 可用、对海外 IP 封禁，反之亦然。因此：

  - 单轮探测失败**不剔除**，只累计（避免因一次网络抖动误杀）
  - 整体成功率低于 `min_probe_success_rate` → 判定为「环境问题」，
    本轮结果整体作废、不写健康档案（避免污染长期数据）
  - 明确剔除只由 L1 静态判定（可靠）+ L3 连续失败（保守）触发
  - 用户反馈黑名单 config/blocklist.json 优先级最高，命中即永久剔除

用法：
  python scripts/curate.py --report            # 只统计，不落盘
  python scripts/curate.py --report --probe    # 统计 + 探测
  python scripts/curate.py --health            # 打印健康档案摘要
  python scripts/curate.py --explain "名字"    # 解释某个源为何被留/被剔
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.request import Request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

DATA_DIR = os.path.join(C.ROOT, "data")
HEALTH_FILE = os.path.join(DATA_DIR, "source_health.json")
POLICY_FILE = os.path.join(C.CONFIG_DIR, "curate.json")
BLOCKLIST_FILE = os.path.join(C.CONFIG_DIR, "blocklist.json")

CATEGORIES = ("direct", "js", "pan", "paid", "magnet", "broken", "relpath", "invalid")

# ──────────────────────────────────────────────────────────────────────
# 默认策略（config/curate.json 缺失时使用；该文件存在时以文件为准）
# ──────────────────────────────────────────────────────────────────────
DEFAULT_POLICY: dict = {
    "version": "1.0.0",
    "note": "源策展策略。改这里即可调整「剔除什么、保留什么、怎么排序」。",
    # relpath = api 是 `./` 相对路径。只有 FongMi 系认，主流 TVBox 官方版不认
    # → 主档必须剔除（否则整份配置「解析配置失败」）。这些源改由 tv-deps.json 提供。
    "drop": ["pan", "paid", "magnet", "broken", "relpath", "invalid"],
    "keep": ["direct", "js"],
    # ── 网盘类指纹：命中即判 pan ──
    #
    # 注意：**刻意不做「域名含 pan. 就判网盘」**。实测大量正常影视源的脚本
    # 托管在网盘域名上（如 `https://pan.szfx.top/down.php/<hash>.js`），
    # 那只是「脚本的存放位置」，资源本身并不依赖网盘会员 —— 按域名判会误杀。
    # 真正可靠的是「接口语义」：URL 里明确写了 type=quark 之类。
    "pan_patterns": [
        # 网盘搜索接口的 query 特征（so.yinpai.xyz/api.php?type=quark 这类）
        r"[?&]type=(?:quark|uc|ali|aliyun|aliyundrive|115|baidu|tianyi|189|"
        r"xunlei|123|mobile|mobil|cloud|pan)\b",
        # 盘搜 / 网盘聚合的路径特征
        r"(?:wanpan|pansou|pan\.php|/pansou/)",
        # alist —— 网盘聚合挂载工具
        r"alist",
        # 阿里云盘授权 token（type=4 推送类常见）
        r"ali_token|aliyundrive_token",
        # 网盘 js 模块（drpy 的 quark.js / uc.js 等）
        r"(?:quark|uc|aliyun|aliyundrive|115|baidu|tianyi|pansou|alipan|"
        r"cloud189|189pan)\.js(?:$|\?)",
    ],
    # ── 名字里的网盘关键词（辅助：须配合「非标准采集接口」才判 pan）──
    "pan_names": [
        "夸克", "网盘", "盘搜", "天翼云盘", "阿里云盘", "百度网盘",
        "迅雷云盘", "115网盘", "123云盘", "UC网盘", "移动云盘", "城通网盘",
        "云盘搜索", "网盘搜索", "盘他", "阿里云盘搜索",
    ],
    # ── group 分组里的网盘标记（上游自己标的，很准；同样排除采集接口）──
    #     实测 `mb资源` 的 group 是「综合-网盘」但 api 是标准 /provide/vod 采集，
    #     属上游误标，必须豁免，否则误杀正经免费源。
    "pan_groups": ["网盘"],
    # ── drpy 引擎文件名：type=3 且 api 只是引擎、又没有 ext 时 = 空壳 ──
    #     实测 307 个这样的源（上游 ext 是 `./deps/...` 相对路径，被清洗删掉），
    #     客户端加载后什么都点不开 —— 用户会误以为是「要会员」。
    "drpy_engines": [
        r"(?:^|/)(?:d?drpy2?|drpy)\.(?:min\.)?m?js$",
    ],
    # ── 付费类指纹：高置信，宁缺勿滥（域名含 vip ≠ 付费，实测 jipinvip.com
    #    等名字带「贵宾」的其实是免费采集站，所以不用域名判）──
    "paid_patterns": [
        r"puqu",                       # 付费源常见标识
        r"[?&]paid=1",
        r'"paid"\s*:\s*true',
        r'"(?:vip_only|need_pay|require_vip)"\s*:\s*true',
    ],
    "paid_names": ["付费专区", "会员专享", "vip专享", "充值专享", "付费点播"],
    # ── 磁力类 ──
    "magnet_patterns": [r"magnet", r"(?:^|[/.])cilixiong", r"(?:^|[/.])jianpian"],
    "magnet_names": ["磁力", "荐片", "种子"],
    # ── 探测参数 ──
    "speed": {
        "probe_enabled": True,
        "probe_timeout_s": 6,
        "probe_concurrency": 32,
        "probe_max_total_s": 180,      # 总时长兜底，超时后放弃剩余探测
        "min_probe_success_rate": 0.15,  # 低于此成功率 → 判为环境问题，本轮作废
        # ── 探测黑名单（安全边界）──────────────────────────────────
        # 这些域名下的地址**一律不发请求**。理由：探测会对目标发真实 HTTP
        # 请求，而一个影视聚合工具不该去戳客服系统 / 短链 / 社交 / 网盘的域名 ——
        # 既探不出影视源的可用性，又会被安全软件按「可疑行为」拦截。
        # 实测事故：上游有源把脚本托管在七陌客服文件服务器
        # （fs-im-kefu.7moor-fs1.com），探测它触发火绒「木马盗号」告警。
        "probe_deny_patterns": [
            "7moor", "kefu", "yunxin", "53kf", "customer",
            "t\\.cn/", "dwz\\.", "url\\.cn", "suo\\.im", "rrd\\.me",
            "weixin\\.qq\\.com", "work\\.weixin", "mp\\.weixin",
            "pan\\.baidu\\.com", "aliyundrive\\.com", "quark\\.cn",
            "taobao\\.com", "alipay\\.com", "weibo\\.com", "douyin\\.com",
            "localhost", "127\\.0\\.0\\.1", "10\\.", "192\\.168\\.", "172\\.16\\.",
        ],
        "paid_keywords": [
            "请充值", "购买会员", "开通会员", "无权限", "请登录",
            "会员专享", "收费", "付费", "not authorized", "unauthorized",
        ],
        "alive_markers": ['"list"', '"class"', '"data"', '"result"', '"code"'],
    },
    # ── 健康档案 / 自我优化 ──
    "health": {
        "ewma_alpha": 0.4,             # 速度 EWMA 平滑系数（越大越信当前值）
        "quarantine_after_fails": 4,   # 连续 N 轮探测失败 → 隔离
        "quarantine_after_rounds": 0,  # 预留：按「出现轮次」隔离（默认关）
    },
}


# ──────────────────────────────────────────────────────────────────────
# 策略 / 黑名单 / 健康档案 读写
# ──────────────────────────────────────────────────────────────────────
def load_policy(path: str = None) -> dict:
    """读策略文件；缺失字段用默认值补齐（向前兼容）。"""
    path = path or POLICY_FILE
    pol = json.loads(json.dumps(DEFAULT_POLICY))  # deep copy
    try:
        with open(path, encoding="utf-8-sig") as f:
            user = json.load(f)
        if isinstance(user, dict):
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(pol.get(k), dict):
                    pol[k].update(v)
                else:
                    pol[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        C.log(f"[curate][WARN] 策略文件解析失败，改用内置默认：{e}")
    return pol


def load_blocklist() -> dict:
    """用户反馈黑名单：{"keys": [...], "apis": [...], "reasons": {...}}。

    这是「自我优化」里唯一的人工闭环入口 —— 用户遇到要会员的源，
    把名字/地址填进来，下一轮起永久剔除。
    """
    try:
        with open(BLOCKLIST_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def load_health() -> dict:
    try:
        with open(HEALTH_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("entries"), dict):
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"version": "1.0.0", "updated_at": C.iso(), "rounds": 0, "entries": {}}


def save_health(h: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    h["updated_at"] = C.iso()
    tmp = HEALTH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(h, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, HEALTH_FILE)


# ──────────────────────────────────────────────────────────────────────
# 指纹
# ──────────────────────────────────────────────────────────────────────
def site_fp(s: dict) -> str:
    """站点指纹：key + 归一化 api。用于跨轮次追踪同一个源。"""
    key = str(s.get("key") or s.get("name") or "").strip().lower()
    api = str(s.get("api") or "").strip().lower()
    return C.sha256_of(f"{key}|{api}")[:16]


# ──────────────────────────────────────────────────────────────────────
# L1 静态分类
# ──────────────────────────────────────────────────────────────────────
def _compile(patterns: list) -> list:
    out = []
    for p in patterns or []:
        try:
            out.append(re.compile(p, re.I))
        except re.error:
            continue
    return out


_PAN_RE_CACHE: dict = {}


def _res(policy: dict) -> dict:
    key = id(policy)
    if key not in _PAN_RE_CACHE:
        _PAN_RE_CACHE[key] = {
            "pan": _compile(policy.get("pan_patterns")),
            "paid": _compile(policy.get("paid_patterns")),
            "magnet": _compile(policy.get("magnet_patterns")),
            "drpy": _compile(policy.get("drpy_engines")),
        }
    return _PAN_RE_CACHE[key]


def _is_collect_api(api: str) -> bool:
    """是否是标准苹果CMS采集接口（这类即使名字带「网盘」也是正经免费采集）。"""
    a = api.lower()
    return ("/provide/vod" in a) or ("api.php?ac=" in a) or ("ac=list" in a and "/api" in a)


def classify_site(s: dict, policy: dict) -> tuple[str, str]:
    """返回 (类别, 理由)。类别见 CATEGORIES。"""
    if not isinstance(s, dict):
        return "invalid", "非对象"
    api = str(s.get("api") or "").strip()
    ext = s.get("ext")
    ext_s = ext if isinstance(ext, str) else json.dumps(ext, ensure_ascii=False) if ext else ""
    name = str(s.get("name") or "")
    group = str(s.get("group") or "")
    blob = f"{api} {ext_s}"
    res = _res(policy)

    if not api:
        return "invalid", "缺 api"

    collect = _is_collect_api(api)

    # ── pan：接口语义特征优先（可靠，不做豁免）──
    for rx in res["pan"]:
        m = rx.search(blob)
        if m:
            return "pan", f"网盘接口特征 /{rx.pattern[:24]}/ 命中「{m.group(0)[:30]}」"
    # group / name 里的网盘词：只对「非标准采集接口」生效（避免误伤天翼影视等采集站）
    if not collect:
        for w in policy.get("pan_groups") or []:
            if w in group:
                return "pan", f"分组「{group}」标记网盘且非标准采集接口"
        for w in policy.get("pan_names") or []:
            if w in name:
                return "pan", f"名称含「{w}」且非标准采集接口"

    # ── paid：高置信特征 ──
    for rx in res["paid"]:
        if rx.search(blob):
            return "paid", f"付费特征 /{rx.pattern[:28]}/"
    for w in policy.get("paid_names") or []:
        if w in name.lower():
            return "paid", f"名称含「{w}」"

    # ── magnet ──
    for rx in res["magnet"]:
        if rx.search(blob):
            return "magnet", f"磁力特征 /{rx.pattern[:28]}/"
    for w in policy.get("magnet_names") or []:
        if w in name:
            return "magnet", f"名称含「{w}」"

    # ── broken：drpy 引擎空壳（引擎在、配置没了 → 点不开）──
    if not ext_s:
        for rx in res["drpy"]:
            if rx.search(api):
                return "broken", f"drpy 引擎「{api.split('/')[-1]}」缺 ext，无法工作"

    # ── relpath：api 是 `./` 同源相对路径（兼容性红线）──────────────
    #   ★ 2026-10-05 事故：为「救活」残缺源，我们把上游的 `./deps/xxx.js`
    #   原样保留进了主档。讴歌 / 影视仓（FongMi 系）能处理，但**主流
    #   TVBox 官方版（手机端）不认相对路径 api → 整份配置「解析配置失败」**。
    #   兼容性优先：主档剔除；这些源改由独立档位 tv-deps.json 提供。
    if api.startswith("./"):
        return "relpath", f"api 为相对路径（{api[:44]}），非 FongMi 系客户端不认"

    # ── js / direct ──
    #   脚本类源：type=3（spider）、api 指向 .js/.mjs/.py 脚本。
    #   其余为直接采集接口（type=0/1）。
    t = s.get("type", 0)
    low = api.lower()
    if t == 3 or low.endswith((".js", ".mjs", ".py")) or "drpy" in low:
        return "js", "脚本源（drpy / QuickJS / Python）"
    return "direct", "http 采集接口"


# ──────────────────────────────────────────────────────────────────────
# 速度标注（上游 hebi 在名字里写过 [NNNms|tag]）
# ──────────────────────────────────────────────────────────────────────
_MS_RE = re.compile(r"\[(\d{2,6})\s*ms", re.I)


def name_speed_ms(s: dict) -> int | None:
    m = _MS_RE.search(str(s.get("name") or ""))
    return int(m.group(1)) if m else None


# ──────────────────────────────────────────────────────────────────────
# L2 主动探测
# ──────────────────────────────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """探测**不跟随重定向**。

    实测事故（2026-10-05）：上游不少源把 jar 托管在七陌客服的文件服务器上
    （`fs-im-kefu.7moor-fs1.com`，伪装成 .png/.txt 对抗 CDN 类型检查），
    某些采集接口会 302 跳到那里。`urllib` 默认**自动跟随重定向** ——
    于是我们的探测请求打到了客服系统域名，被火绒按可疑行为报「木马盗号」。

    探测只需要看第一次响应：不跟随既更安全（不会跳到第三方域名），
    也更准确（3xx 本身就说明这个接口不是标准采集端点）。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_PROBE_OPENER = urllib.request.build_opener(_NoRedirect)


# ── 探测目标的准入判定（安全边界，2026-10-05 收紧）──────────────────
#
# 为什么必须收紧：探测会对每个目标发**真实 HTTP 请求**。上游源的 api
# 字段五花八门，实测有把脚本托管在第三方**客服系统文件服务器**上的
# （`fs-im-kefu.7moor-fs1.com`）—— 探测它会被安全软件按「可疑行为」拦截，
# 火绒直接报「木马盗号」。虽然是误报，但一个影视聚合工具不该去戳
# 客服系统、短链、社交平台的域名。
#
# 而且这类「脚本类 api」本来也探测不出可用性（返回的是 JS 源码，
# 不是数据接口），去掉它既更安全、也更准确。
_NON_PROBE_SUFFIX = re.compile(
    r"\.(?:js|mjs|py|json|txt|css|html?|xml|jpg|jpeg|png|gif|webp|"
    r"mp4|m3u8|ts|zip|rar|apk|jar)(?:\?|$)", re.I)
_COLLECT_API_HINT = re.compile(
    r"(?:provide/vod|/api\.php|/vod\.php|[\?&]ac=(?:list|detail|videolist)|"
    r"/vod/|/api/|/provide/)", re.I)


def is_probeable(api: str, policy: dict = None) -> bool:
    """这个地址该不该被主动探测。

    三重门（全部通过才探测）：
      1. 必须是 http(s) 绝对地址
      2. **不是**脚本/静态文件后缀（.js/.py/.json/...）
      3. **像**采集接口（含 provide/vod、api.php、ac= 等特征）
      4. 不命中策略里的 `probe_deny_patterns`（客服/短链/社交等第三方域名）
    """
    a = (api or "").strip()
    if not a.startswith(("http://", "https://")):
        return False
    if _NON_PROBE_SUFFIX.search(a):
        return False                      # 脚本或静态文件，不是接口
    deny = ((policy or {}).get("speed") or {}).get("probe_deny_patterns") or []
    for pat in deny:
        try:
            if re.search(pat, a, re.I):
                return False
        except re.error:
            continue
    return bool(_COLLECT_API_HINT.search(a))


def _probe_url(api: str) -> str:
    """把采集接口补上 `ac=list`（列分类），这是最轻量的存活探测请求。"""
    a = api.strip()
    if not a.startswith(("http://", "https://")):
        return ""
    if "ac=" in a:
        return a
    return a + ("&ac=list" if "?" in a else "?ac=list")


def probe_one(s: dict, policy: dict) -> dict:
    """探测单个源。返回 {ok, ms, code, paid, err}。"""
    sp = policy.get("speed") or {}
    timeout = float(sp.get("probe_timeout_s", 6))
    api = str(s.get("api") or "")
    if not is_probeable(api, policy):
        # 双保险：不在准入范围内的地址一律不发请求
        return {"ok": False, "ms": None, "code": None, "paid": False,
                "err": "skip:非采集接口"}
    url = _probe_url(api)
    if not url:
        return {"ok": False, "ms": None, "code": None, "paid": False, "err": "非 http 接口"}
    if not C.url_encodable(url):
        return {"ok": False, "ms": None, "code": None, "paid": False, "err": "URL 不可编码"}
    t0 = time.time()
    try:
        req = Request(url, headers={
            "User-Agent": "okhttp/3.12.0",
            "Accept-Encoding": "identity",
        })
        # 用不跟随重定向的 opener（见 _NoRedirect 的说明）
        with _PROBE_OPENER.open(req, timeout=timeout) as r:
            body = r.read(4096)
            code = r.status
        ms = int((time.time() - t0) * 1000)
        text = body.decode("utf-8", "ignore")
        low = text.lower()
        # 付费特征
        for kw in (sp.get("paid_keywords") or []):
            if kw.lower() in low:
                return {"ok": False, "ms": ms, "code": code, "paid": True,
                        "err": f"响应含「{kw}」"}
        # 存活特征：返回体里出现数据标记
        alive = any(mk.lower() in low for mk in (sp.get("alive_markers") or []))
        if code == 200 and (alive or len(body) > 0):
            return {"ok": True, "ms": ms, "code": code, "paid": False, "err": ""}
        return {"ok": False, "ms": ms, "code": code, "paid": False,
                "err": f"HTTP {code} 但无有效数据"}
    except HTTPError as e:
        # 注意：401/402/403 **不判为付费**。实测 403 的常见原因是
        # WAF 拦截、UA/Referer 校验、封锁机房 IP —— 直接判「要钱」会误杀。
        # 只有响应体里明确出现付费字样才归为 paid（见上面的 paid_keywords）。
        ms = int((time.time() - t0) * 1000)
        return {"ok": False, "ms": ms, "code": e.code, "paid": False,
                "err": f"HTTP {e.code}"}
    except URLError as e:
        ms = int((time.time() - t0) * 1000)
        return {"ok": False, "ms": ms, "code": None, "paid": False, "err": f"URLError {e.reason}"[:80]}
    except Exception as e:  # noqa: BLE001
        ms = int((time.time() - t0) * 1000)
        return {"ok": False, "ms": ms, "code": None, "paid": False, "err": f"{type(e).__name__}"[:60]}


def probe_many(sites: list, policy: dict) -> tuple[dict, dict]:
    """并发探测。返回 (fp→结果, 统计)。带总时长兜底。"""
    sp = policy.get("speed") or {}
    conc = int(sp.get("probe_concurrency", 32))
    budget = float(sp.get("probe_max_total_s", 180))
    t0 = time.time()
    results: dict = {}
    stats = {"probed": 0, "ok": 0, "paid": 0, "fail": 0, "skipped_budget": 0}
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = {}
        for s in sites:
            if time.time() - t0 > budget:
                stats["skipped_budget"] += 1
                continue
            futs[ex.submit(probe_one, s, policy)] = site_fp(s)
        for fu in as_completed(futs):
            fp = futs[fu]
            try:
                r = fu.result()
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "ms": None, "code": None, "paid": False,
                     "err": f"{type(e).__name__}"}
            results[fp] = r
            stats["probed"] += 1
            if r.get("paid"):
                stats["paid"] += 1
            elif r.get("ok"):
                stats["ok"] += 1
            else:
                stats["fail"] += 1
    stats["elapsed_s"] = round(time.time() - t0, 1)
    return results, stats


# ──────────────────────────────────────────────────────────────────────
# L3 健康档案更新（自我优化）
# ──────────────────────────────────────────────────────────────────────
def update_health(health: dict, sites: list, probe_results: dict,
                  policy: dict, stats: dict) -> None:
    """把本轮探测结果折进健康档案。

    熔断：整体成功率过低 → 判为探测环境问题，本轮不写档案。
    """
    sp = policy.get("speed") or {}
    hc = policy.get("health") or {}
    min_rate = float(sp.get("min_probe_success_rate", 0.15))
    alpha = float(hc.get("ewma_alpha", 0.4))
    thr = int(hc.get("quarantine_after_fails", 4))

    probed = int(stats.get("probed") or 0)
    ok = int(stats.get("ok") or 0)
    if probed and (ok / probed) < min_rate:
        stats["health_circuit_breaker"] = True
        C.log(f"[curate] 探测成功率 {ok}/{probed} 低于阈值 {min_rate:.0%} "
              f"—— 判为环境问题，本轮结果不写入健康档案")
        return

    entries = health.setdefault("entries", {})
    now = C.iso()
    for s in sites:
        fp = site_fp(s)
        r = probe_results.get(fp)
        if r is None:
            continue
        e = entries.get(fp)
        if e is None:
            e = {"key": str(s.get("key") or s.get("name") or "")[:60],
                 "api_host": _host(str(s.get("api") or "")),
                 "seen": 0, "ok": 0, "fail": 0, "fails_streak": 0,
                 "ms_ewma": None, "last_ok": None, "last_check": None,
                 "quarantine": False}
            entries[fp] = e
        e["seen"] = int(e.get("seen", 0)) + 1
        e["last_check"] = now
        if r.get("ok"):
            e["ok"] = int(e.get("ok", 0)) + 1
            e["fails_streak"] = 0
            e["last_ok"] = now
            e["quarantine"] = False
            ms = r.get("ms")
            if isinstance(ms, int):
                old = e.get("ms_ewma")
                e["ms_ewma"] = ms if old is None else int(alpha * ms + (1 - alpha) * old)
        else:
            e["fail"] = int(e.get("fail", 0)) + 1
            e["fails_streak"] = int(e.get("fails_streak", 0)) + 1
            if r.get("paid"):
                # 探测发现「要钱」：直接隔离，不必等连续失败
                e["quarantine"] = True
                e["quarantine_reason"] = f"探测判定付费：{r.get('err')}"
            elif e["fails_streak"] >= thr:
                e["quarantine"] = True
                e["quarantine_reason"] = f"连续 {e['fails_streak']} 轮探测失败"
    health["rounds"] = int(health.get("rounds", 0)) + 1


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1)[:60] if m else ""


# ──────────────────────────────────────────────────────────────────────
# 排序
# ──────────────────────────────────────────────────────────────────────
def speed_key(s: dict, health: dict) -> tuple:
    """越靠前 = 越可能「免费且高速」。

    优先级：上游标注 ms → 健康档案 EWMA ms → 无数据（保持原序）。
    """
    ms = name_speed_ms(s)
    if ms is not None:
        return (0, ms)
    e = (health.get("entries") or {}).get(site_fp(s)) or {}
    ew = e.get("ms_ewma")
    if isinstance(ew, int) and ew > 0:
        return (1, ew)
    # 有成功史的排在没有的之前
    if int(e.get("ok", 0)) > 0:
        return (2, 0)
    return (3, 0)


# ──────────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────────
def curate(sites: list, policy: dict = None, health: dict = None,
           do_probe: bool = None, blocklist: dict = None) -> dict:
    """执行策展。返回 {kept, dropped, stats}。不落盘，不写档案。"""
    policy = policy or load_policy()
    health = health if health is not None else load_health()
    blocklist = blocklist if blocklist is not None else load_blocklist()
    drop_cats = set(policy.get("drop") or [])

    bl_keys = {str(x).strip().lower() for x in (blocklist.get("keys") or [])}
    bl_apis = {str(x).strip().lower() for x in (blocklist.get("apis") or [])}

    kept: list = []
    dropped: list = []
    by_cat: dict = {c: 0 for c in CATEGORIES}
    reasons: dict = {}

    for s in sites:
        cat, why = classify_site(s, policy)
        by_cat[cat] = by_cat.get(cat, 0) + 1
        key = str(s.get("key") or s.get("name") or "").strip().lower()
        api = str(s.get("api") or "").strip().lower()

        if key and key in bl_keys:
            dropped.append({"key": s.get("key"), "name": s.get("name"),
                            "category": "blocklist", "reason": "用户反馈黑名单（名称）"})
            reasons["blocklist"] = reasons.get("blocklist", 0) + 1
            continue
        if api and api in bl_apis:
            dropped.append({"key": s.get("key"), "name": s.get("name"),
                            "category": "blocklist", "reason": "用户反馈黑名单（地址）"})
            reasons["blocklist"] = reasons.get("blocklist", 0) + 1
            continue

        if cat in drop_cats:
            dropped.append({"key": s.get("key"), "name": s.get("name"),
                            "category": cat, "reason": why})
            reasons[cat] = reasons.get(cat, 0) + 1
            continue

        e = (health.get("entries") or {}).get(site_fp(s)) or {}
        if e.get("quarantine"):
            dropped.append({"key": s.get("key"), "name": s.get("name"),
                            "category": "quarantine",
                            "reason": e.get("quarantine_reason") or "历史探测持续失败"})
            reasons["quarantine"] = reasons.get("quarantine", 0) + 1
            continue

        kept.append(s)

    # 排序：快在前
    kept.sort(key=lambda s: speed_key(s, health))

    stats = {
        "in": len(sites), "kept": len(kept), "dropped": len(dropped),
        "by_category": by_cat, "drop_reasons": reasons,
        "policy_version": policy.get("version"),
    }
    return {"kept": kept, "dropped": dropped, "stats": stats}


def apply(sites: list, stats: dict = None, policy: dict = None,
          build: bool = False) -> list:
    """供 merge.py 调用的接入点：策展 + （build 时）探测并更新健康档案。

    - 非 build：只做静态策展（快，测试用）
    - build：静态策展 + 探测 + 写 data/source_health.json
    """
    policy = policy or load_policy()
    health = load_health()
    do_probe = bool((policy.get("speed") or {}).get("probe_enabled")) and build

    probe_results: dict = {}
    pstats: dict = {}
    if do_probe and sites:
        # 只探测「真正的采集接口」（is_probeable 三重门）：
        # 脚本类/静态文件类 api 不发请求 —— 既探不出可用性，
        # 又会因访问第三方域名触发安全软件的误报。
        targets = [s for s in sites if is_probeable(str(s.get("api") or ""), policy)]
        C.log(f"[curate] 开始探测 {len(targets)} 个 http 接口 ...")
        probe_results, pstats = probe_many(targets, policy)
        C.log(f"[curate] 探测完成：ok={pstats.get('ok')} paid={pstats.get('paid')} "
              f"fail={pstats.get('fail')} 耗时={pstats.get('elapsed_s')}s")
        update_health(health, targets, probe_results, policy, pstats)
        if not pstats.get("health_circuit_breaker"):
            save_health(health)

    r = curate(sites, policy, health)
    out_stats = r["stats"]
    out_stats["probe"] = pstats
    if stats is not None:
        for k, v in out_stats.items():
            stats[f"curate_{k}"] = v
    if build:
        write_report(r, pstats, health)
    return r["kept"]


def write_report(r: dict, pstats: dict, health: dict) -> None:
    """把策展明细落盘到 public/curate-report.json（透明可查）。

    为什么要公开：用户有权利知道「我的源被筛掉了什么、为什么」。
    这份报告也让「自我优化」可审计 —— 隔离了哪些、什么原因，一目了然。
    """
    entries = health.get("entries") or {}
    quarantined = [{"key": e.get("key"), "api_host": e.get("api_host"),
                    "reason": e.get("quarantine_reason"), "fails_streak": e.get("fails_streak")}
                   for e in entries.values() if e.get("quarantine")]
    data = {
        "at": C.iso(), "at_bj": C.bjnow(),
        "stats": r["stats"],
        "probe": pstats,
        "health": {"rounds": health.get("rounds"), "entries": len(entries),
                   "quarantined": len(quarantined)},
        "quarantined": quarantined[:200],
        # 剔除明细（最多 400 条，按类别归组，便于人工复核）
        "dropped": r["dropped"][:400],
    }
    try:
        C.write_json(os.path.join(C.PUBLIC_DIR, "curate-report.json"), data)
    except Exception as e:  # noqa: BLE001
        C.log(f"[curate] 写 curate-report.json 失败（不影响构建）：{e}")


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────
def _load_sites(files: list) -> list:
    out: list = []
    for f in files:
        if not os.path.exists(f):
            # 相对 public/ 找
            f2 = os.path.join(C.PUBLIC_DIR, f)
            f = f2 if os.path.exists(f2) else f
        try:
            with open(f, encoding="utf-8-sig") as fh:
                d = json.load(fh)
            out += list(d.get("sites") or [])
        except Exception as e:  # noqa: BLE001
            C.log(f"[curate][WARN] 读取 {f} 失败：{e}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="tv-hub 源策展器")
    ap.add_argument("--report", action="store_true", help="只统计")
    ap.add_argument("--probe", action="store_true", help="启用主动探测")
    ap.add_argument("--files", nargs="*", default=["tv.json"])
    ap.add_argument("--health", action="store_true", help="打印健康档案摘要")
    ap.add_argument("--explain", default=None, help="解释某个源（按名字模糊匹配）")
    ap.add_argument("--dump-dropped", default=None, help="把被剔除的清单写到指定文件")
    a = ap.parse_args()

    policy = load_policy()

    if a.health:
        h = load_health()
        entries = h.get("entries") or {}
        q = [e for e in entries.values() if e.get("quarantine")]
        with_ms = [e for e in entries.values() if isinstance(e.get("ms_ewma"), int)]
        C.log(f"[health] 轮次={h.get('rounds')} 条目={len(entries)} "
              f"已隔离={len(q)} 有速度数据={len(with_ms)}")
        if with_ms:
            vals = sorted(e["ms_ewma"] for e in with_ms)
            C.log(f"[health] EWMA 速度：最快={vals[0]}ms 中位={vals[len(vals)//2]}ms 最慢={vals[-1]}ms")
        for e in q[:20]:
            C.log(f"   ⛔ {e.get('key')} @{e.get('api_host')} —— {e.get('quarantine_reason')}")
        return 0

    sites = _load_sites(a.files)
    if not sites:
        C.log("[curate][FATAL] 没有读到任何站点")
        return 2

    if a.explain:
        pat = a.explain.lower()
        hits = [s for s in sites if pat in str(s.get("name") or "").lower()
                or pat in str(s.get("key") or "").lower()]
        C.log(f"[explain] 匹配 {len(hits)} 个：")
        for s in hits[:20]:
            cat, why = classify_site(s, policy)
            C.log(f"   [{cat:8s}] {s.get('name')} | {str(s.get('api'))[:60]} —— {why}")
        return 0

    health = load_health()
    if a.probe:
        targets = [s for s in sites if is_probeable(str(s.get("api") or ""), policy)]
        C.log(f"[curate] 探测 {len(targets)} 个 http 接口 ...")
        pr, ps = probe_many(targets, policy)
        C.log(f"[curate] ok={ps.get('ok')} paid={ps.get('paid')} fail={ps.get('fail')} "
              f"耗时={ps.get('elapsed_s')}s")
        update_health(health, targets, pr, policy, ps)
        if not ps.get("health_circuit_breaker"):
            save_health(health)
            C.log(f"[curate] 健康档案已更新：{HEALTH_FILE}")

    r = curate(sites, policy, health)
    st = r["stats"]
    C.log(f"[curate] 输入 {st['in']} → 保留 {st['kept']} / 剔除 {st['dropped']}")
    C.log(f"[curate] 分类：{json.dumps(st['by_category'], ensure_ascii=False)}")
    C.log(f"[curate] 剔除原因：{json.dumps(st['drop_reasons'], ensure_ascii=False)}")
    for x in r["dropped"][:15]:
        C.log(f"   ✗ [{x['category']}] {x.get('name')} —— {x['reason']}")
    if a.dump_dropped:
        with open(a.dump_dropped, "w", encoding="utf-8", newline="\n") as f:
            json.dump(r["dropped"], f, ensure_ascii=False, indent=2)
            f.write("\n")
        C.log(f"[curate] 剔除清单已写入 {a.dump_dropped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
