"""curate.py 的回归测试。

覆盖「筛掉不能用的源」这条主线的关键判据 —— 每一条都对应一个真实事故：
  - 网盘源的误判与豁免（`mb资源` 曾被 group 误标误杀）
  - drpy 引擎空壳（307 个源曾经点了没反应）
  - 相对路径 ext 不算空壳（一刀切删除的教训）
  - 黑名单优先于自动判定
  - 探测熔断（避免机房网络视角误杀国内可用源）
"""
from __future__ import annotations

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import common as C      # noqa: E402
import curate as CU     # noqa: E402

# 原样发布的第三方基线（public/tv-ysc.json）不做任何加工，
# 用途是与我们自己的产物做对照 —— 产物守卫测试必须跳过它。
BASELINE_SKIP = {"tv-ysc.json"}


def _site(**kw) -> dict:
    """默认构造一个「标准采集站」；要测脚本源请显式传 type=3。"""
    base = {"key": "k", "name": "n", "type": 1,
            "api": "https://a.com/api.php/provide/vod?ac=list"}
    base.update(kw)
    return base


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.pol = CU.load_policy()

    def cat(self, s):
        return CU.classify_site(s, self.pol)[0]

    # ── 网盘类 ──
    def test_pan_by_query_semantics(self):
        """`?type=quark` 这类接口语义是网盘搜索的可靠信号。"""
        for t in ("quark", "uc", "aliyun", "tianyi", "baidu", "xunlei", "115", "123"):
            s = _site(api=f"https://so.example.com/api.php?type={t}", name="某源")
            self.assertEqual(self.cat(s), "pan", f"type={t} 应判 pan")

    def test_pan_by_alist_and_wanpan(self):
        self.assertEqual(self.cat(_site(api="http://alist.x.com/ddrpy2.m.js", name="直播")), "pan")
        self.assertEqual(self.cat(_site(
            api="http://tvbox.zling.vip/wanpan.php?type=quark|uc",
            name="盘搜|集合")), "pan")

    def test_pan_by_group(self):
        s = _site(api="https://pan.szfx.top/down.php/abc.js", name="枫叶", group="综合-网盘")
        self.assertEqual(self.cat(s), "pan")

    def test_collect_api_exempt_from_group_and_name(self):
        """★ 关键豁免：group/名称带「网盘」但接口是标准采集站的，必须保留。

        实测上游把 `mb资源`（api 为 /provide/vod 的正经采集站）的 group
        错标成「综合-网盘」。按 group 一刀切会误杀。
        """
        s = _site(api="http://124.222.30.115/mb/api.php/provide/vod",
                  name="mb资源", group="综合-网盘")
        self.assertEqual(self.cat(s), "direct")
        # 「天翼影视」是采集站名，不该因含「天翼」被当网盘
        s2 = _site(api="https://api.tianyi.com/api.php/provide/vod/at/json",
                   name="天翼影视", group="综合-采集站")
        self.assertEqual(self.cat(s2), "direct")

    def test_pan_hostname_alone_not_enough(self):
        """★ 脚本托管在网盘域名上 ≠ 资源依赖网盘会员。"""
        s = _site(api="https://pan.29o.cn/down.php/2897f8eeef.py", name="雷子")
        # 没有 group/名称/query 语义佐证时，不因域名判 pan
        self.assertNotEqual(self.cat(s), "pan")

    # ── 磁力 / 付费 ──
    def test_magnet(self):
        self.assertEqual(self.cat(_site(
            api="http://rihou.vip:55/lib/drpy2.min.js",
            ext="http://rihou.vip:55/lib/jianpian.js", name="荐片")), "magnet")
        self.assertEqual(self.cat(_site(name="磁力大全")), "magnet")

    def test_paid_static(self):
        self.assertEqual(self.cat(_site(api="https://puqu.example.com/api")), "paid")
        self.assertEqual(self.cat(_site(name="会员专享", api="https://a.com/x")), "paid")

    # ── 空壳 ──
    def test_broken_drpy_engine_without_ext(self):
        s = _site(type=3, api="https://gitcode.net/x/dr_py/-/raw/master/libs/drpy2.min.js",
                  name="1080P[js]")
        self.assertEqual(self.cat(s), "broken")

    def test_relative_ext_is_not_broken(self):
        """★ 相对路径 ext 是有效的（同源部署下可用），不能判空壳。"""
        s = _site(type=3, api="https://gitcode.net/x/dr_py/-/raw/master/libs/drpy2.min.js",
                  ext="./deps/localized/b7267cce5f-1080P.js", name="1080P[js]")
        self.assertEqual(self.cat(s), "js")

    def test_script_as_api_needs_no_ext(self):
        """api 本身就是脚本（.py / 具体 .js）时，没有 ext 也是完整的。"""
        s = _site(type=3, api="https://cdn.x.com/py/py_douban.py", name="豆瓣")
        self.assertEqual(self.cat(s), "js")

    def test_invalid_without_api(self):
        self.assertEqual(self.cat({"key": "k", "name": "n"}), "invalid")


class TestCurateFlow(unittest.TestCase):
    def setUp(self):
        self.pol = CU.load_policy()
        self.empty_health = {"version": "1.0.0", "rounds": 0, "entries": {}}

    def test_curate_drops_and_keeps(self):
        sites = [
            _site(key="a", api="https://ok.com/api.php/provide/vod?ac=list", name="正常采集"),
            _site(key="b", api="https://so.x.com/api.php?type=quark", name="夸克搜索"),
            _site(key="c", api="https://x.com/drpy2.min.js", name="空壳"),
            _site(key="d", api="http://rihou.vip/lib/jianpian.js", name="荐片磁力"),
        ]
        r = CU.curate(sites, self.pol, self.empty_health, blocklist={})
        kept = [s["key"] for s in r["kept"]]
        dropped = {x["key"] for x in r["dropped"]}
        self.assertEqual(kept, ["a"])
        self.assertEqual(dropped, {"b", "c", "d"})

    def test_blocklist_beats_everything(self):
        """黑名单（用户反馈）优先级最高，高于任何自动判定。"""
        sites = [_site(key="good", api="https://ok.com/api.php/provide/vod?ac=list")]
        r = CU.curate(sites, self.pol, self.empty_health,
                      blocklist={"keys": ["good"]})
        self.assertEqual(r["kept"], [])
        self.assertEqual(r["dropped"][0]["category"], "blocklist")

    def test_quarantine_respected_and_releasable(self):
        s = _site(key="q", api="https://ok.com/api.php/provide/vod?ac=list")
        fp = CU.site_fp(s)
        h = {"entries": {fp: {"key": "q", "quarantine": True,
                              "quarantine_reason": "连续 4 轮探测失败"}}}
        r = CU.curate([s], self.pol, h, blocklist={})
        self.assertEqual(r["kept"], [])
        # 解除隔离（人工删条目）后立即恢复
        r2 = CU.curate([s], self.pol, {"entries": {}}, blocklist={})
        self.assertEqual(len(r2["kept"]), 1)

    def test_speed_sorting(self):
        sites = [
            _site(key="slow", name="[5000ms|x] 慢的", api="https://a.com/api.php/provide/vod?ac=list"),
            _site(key="fast", name="[200ms|x] 快的", api="https://b.com/api.php/provide/vod?ac=list"),
            _site(key="mid", name="没有标注", api="https://c.com/api.php/provide/vod?ac=list"),
        ]
        r = CU.curate(sites, self.pol, self.empty_health, blocklist={})
        self.assertEqual([s["key"] for s in r["kept"]], ["fast", "slow", "mid"])


class TestHealth(unittest.TestCase):
    def test_circuit_breaker_discards_round(self):
        """★ 探测成功率过低 = 环境问题，本轮结果必须整体作废。

        否则 GitHub 机房视角会污染长期数据，把国内可用源逐步误杀。
        """
        pol = CU.load_policy()
        health = {"rounds": 0, "entries": {}}
        sites = [_site(key=f"s{i}", api=f"https://h{i}.com/api.php/provide/vod?ac=list")
                 for i in range(20)]
        # 只有 1 个成功 → 成功率 5% < 15% 阈值
        results = {CU.site_fp(s): {"ok": False, "ms": None, "code": None, "paid": False, "err": "x"}
                   for s in sites}
        results[CU.site_fp(sites[0])] = {"ok": True, "ms": 100, "code": 200, "paid": False, "err": ""}
        stats = {"probed": 20, "ok": 1, "fail": 19, "paid": 0}
        CU.update_health(health, sites, results, pol, stats)
        self.assertTrue(stats.get("health_circuit_breaker"))
        self.assertEqual(health["entries"], {})       # 未写入任何条目

    def test_ewma_and_quarantine(self):
        pol = CU.load_policy()
        health = {"rounds": 0, "entries": {}}
        s = _site(key="e", api="https://e.com/api.php/provide/vod?ac=list")
        fp = CU.site_fp(s)
        # 第一轮成功
        CU.update_health(health, [s], {fp: {"ok": True, "ms": 500, "code": 200,
                                            "paid": False, "err": ""}}, pol,
                         {"probed": 1, "ok": 1, "fail": 0, "paid": 0})
        self.assertEqual(health["entries"][fp]["ms_ewma"], 500)
        # 连续失败达到阈值 → 隔离
        thr = (pol.get("health") or {}).get("quarantine_after_fails", 4)
        for _ in range(thr):
            CU.update_health(health, [s], {fp: {"ok": False, "ms": None, "code": None,
                                                "paid": False, "err": "timeout"}}, pol,
                             {"probed": 1, "ok": 1, "fail": 0, "paid": 0})
        self.assertTrue(health["entries"][fp]["quarantine"])
        # 一旦成功 → 立即恢复
        CU.update_health(health, [s], {fp: {"ok": True, "ms": 300, "code": 200,
                                            "paid": False, "err": ""}}, pol,
                         {"probed": 1, "ok": 1, "fail": 0, "paid": 0})
        self.assertFalse(health["entries"][fp]["quarantine"])

    def test_paid_detected_by_probe_isolates(self):
        pol = CU.load_policy()
        health = {"rounds": 0, "entries": {}}
        s = _site(key="p", api="https://p.com/api.php/provide/vod?ac=list")
        fp = CU.site_fp(s)
        CU.update_health(health, [s], {fp: {"ok": False, "ms": 100, "code": 200,
                                            "paid": True, "err": "响应含「请充值」"}}, pol,
                         {"probed": 1, "ok": 1, "fail": 0, "paid": 1})
        self.assertTrue(health["entries"][fp]["quarantine"])


class TestProbeHelpers(unittest.TestCase):
    def test_probe_url_appends_ac_list(self):
        self.assertEqual(CU._probe_url("https://a.com/api.php/provide/vod"),
                         "https://a.com/api.php/provide/vod?ac=list")
        self.assertEqual(CU._probe_url("https://a.com/api.php/provide/vod?x=1"),
                         "https://a.com/api.php/provide/vod?x=1&ac=list")
        # 已有 ac= 的不重复添加
        u = "https://a.com/api.php/provide/vod?ac=detail"
        self.assertEqual(CU._probe_url(u), u)
        # 非 http 无法探测
        self.assertEqual(CU._probe_url("./deps/x.js"), "")
        self.assertEqual(CU._probe_url("csp_XBPQ"), "")

    def test_name_speed(self):
        self.assertEqual(CU.name_speed_ms({"name": "[137ms|zy] 黑料"}), 137)
        self.assertEqual(CU.name_speed_ms({"name": "[4727ms|2] 天翼"}), 4727)
        self.assertIsNone(CU.name_speed_ms({"name": "没有标注"}))


class TestOutputWhitelist(unittest.TestCase):
    """产物里的站点字段必须全在契约白名单内。

    ★ 2026-10-05 兼容性红线：
      上游夹带 reference / discovery_decoded_works / 中文 key「类型」/
      title / id / order_num / playurl（小写）等私有字段。
      FongMi 系（讴歌、影视仓）忽略未知字段，所以此前没暴露；
      但**主流 TVBox 官方版（手机端）会因未知字段直接判「解析配置失败」**。

      这条测试是回归闸门：任何未知字段混进产物即失败。
    """

    def test_no_unknown_site_fields(self):
        pub = os.path.join(ROOT, "public")
        if not os.path.isdir(pub):
            self.skipTest("尚未构建")
        allow = C.site_allow_fields()
        self.assertTrue(allow, "契约未加载到字段白名单")
        bad: dict = {}
        for fn in sorted(os.listdir(pub)):
            if not fn.endswith(".json") or fn in BASELINE_SKIP:
                continue
            try:
                with open(os.path.join(pub, fn), encoding="utf-8-sig") as f:
                    d = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(d, dict):
                continue
            for s in (d.get("sites") or []):
                if not isinstance(s, dict):
                    continue
                unknown = [k for k in s if k not in allow]
                if unknown:
                    bad.setdefault(fn, set()).update(unknown)
        self.assertFalse(
            bad, "产物出现非白名单字段（主流 TVBox 会判「解析配置失败」）："
                 + json.dumps({k: sorted(v) for k, v in bad.items()}, ensure_ascii=False))

    def test_parse_ext_is_object_not_string(self):
        """★ 产物里 parses[].ext 必须是**对象**，不能是字符串。

        2026-10-05 事故：`Parse.ext` 在 FongMi 里是对象（Ext{flag,header}），
        而 `Parse.objectFrom` **没有 try-catch**（不像 Site.objectFrom 会吞异常）
        —— 上游把 ext 写成 JSON 字符串时，gson 抛异常直接冒泡，
        表现是**整份配置「解析配置失败」**（实测 t4 档稳定复现）。

        对照：能加载的 ysc 配置里 parses[].ext 全部是对象。
        """
        pub = os.path.join(ROOT, "public")
        if not os.path.isdir(pub):
            self.skipTest("尚未构建")
        bad = {}
        for fn in sorted(os.listdir(pub)):
            if not fn.endswith(".json") or fn in BASELINE_SKIP:
                continue
            try:
                with open(os.path.join(pub, fn), encoding="utf-8-sig") as f:
                    d = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(d, dict):
                continue
            for p in (d.get("parses") or []):
                ext = p.get("ext") if isinstance(p, dict) else None
                if ext is not None and not isinstance(ext, dict):
                    bad.setdefault(fn, []).append(
                        {"name": p.get("name"), "ext_type": type(ext).__name__})
        self.assertFalse(
            bad, "parses[].ext 必须是对象（字符串会让整份配置解析失败）："
                 + json.dumps(bad, ensure_ascii=False))

    def test_relative_api_absent_from_main_tiers(self):
        """主档（/tv、/fast、/lite、/standard）不得含 `./` 相对路径 api。

        只有 FongMi 系认相对路径 api，主流 TVBox 官方版不认。
        这些源应只出现在 tv-deps.json。
        """
        pub = os.path.join(ROOT, "public")
        if not os.path.isdir(pub):
            self.skipTest("尚未构建")
        for fn in ("tv.json", "tv-fast.json", "tv-lite.json", "tv-standard.json"):
            p = os.path.join(pub, fn)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8-sig") as f:
                d = json.load(f)
            rel = [s.get("key") for s in (d.get("sites") or [])
                   if str(s.get("api") or "").startswith("./")]
            self.assertFalse(rel, f"{fn} 含相对路径 api（应移入 tv-deps.json）：{rel[:5]}")


if __name__ == "__main__":
    unittest.main()
