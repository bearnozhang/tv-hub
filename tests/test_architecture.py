"""架构层测试：契约单一真源 / 质量门禁 / 线上巡检。

这三套机制是「一劳永逸」的支点，必须被测试锁住，否则日后很容易被改回原样：

1. **契约单一真源** —— 字段类型表只能存在于 contract/kernel.json，
   代码里出现第二份（曾经有三份：sanitize / validate / tests）即视为回归。
2. **质量门禁** —— 用合成数据验证它真的拦得住坏产物。
   拦不住的门禁等于没有。
3. **巡检判定** —— 把网络 mock 掉，只验判定逻辑（含 Content-Type 误判这类历史坑）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import gate as G        # noqa: E402
import kernel as K      # noqa: E402
import sanitize as S    # noqa: E402
import watchdog as W    # noqa: E402

NL = chr(10)


class TestContractIsSingleSource(unittest.TestCase):
    """契约只能在 contract/kernel.json 定义一次。"""

    def test_contract_has_all_sections(self):
        c = K.contract()
        for sec in ("site", "parse", "live", "url", "text", "quality", "watchdog"):
            self.assertIn(sec, c, f"契约缺少 {sec} 段")
        for spec in ("site", "parse", "live"):
            self.assertTrue(c[spec].get("str"), f"{spec} 缺少 str 字段表")
            self.assertTrue((c[spec].get("key") or {}).get("field"),
                            f"{spec} 缺少 key 定义")
        self.assertTrue(c["site"].get("required"), "site 缺少必填字段定义")

    def test_no_second_field_table_in_code(self):
        """扫描 scripts/：不得再出现硬编码字段表常量。"""
        banned = ("SITE_STR", "SITE_INT", "PARSE_STR", "PARSE_INT")
        hits = []
        sdir = os.path.join(ROOT, "scripts")
        for fn in sorted(os.listdir(sdir)):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(sdir, fn), encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if any(b in line for b in banned):
                        hits.append(f"{fn}:{i}: {line.strip()[:70]}")
        self.assertEqual(hits, [],
                         "发现第二份字段表（应统一从契约读取）：" + NL + NL.join(hits))

    def test_kernel_self_test_runs(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "kernel.py"),
                            "--self-test"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, f"kernel 自检未通过：{r.stdout}{r.stderr}")

    def test_detects_the_two_historical_dirty_shapes(self):
        """契约必须识别历史上真正导致线上解析失败的两类脏数据。"""
        dirty_categories = {"key": "a", "name": "A", "type": 0,
                            "categories": "电影,电视剧"}      # 实测 22 处
        dirty_ext = {"key": "b", "name": "B", "type": 3,
                     "api": "http://x/y.js", "ext": {"host": "http://h:1"}}   # 实测 13 处
        self.assertTrue(K.site_conflicts(dirty_categories), "categories 字符串未被识别")
        self.assertTrue(K.site_conflicts(dirty_ext), "ext 对象未被识别")
        self.assertFalse(K.site_conflicts({"key": "c", "name": "C", "type": 0,
                                           "categories": ["电影"], "ext": "x"}),
                         "合法站点被误判")

    def test_sanitizer_closes_the_loop_with_contract(self):
        """凡是契约拒绝的，消毒器必须能修好 —— 两者互为闭环。"""
        samples = [
            {"key": "a", "name": "A", "type": 0, "categories": "电影,电视剧"},
            {"key": "b", "name": "B", "type": 3, "api": "http://x/y.js",
             "ext": {"host": "http://h:1"}},
            {"key": "c", "name": "C", "type": "1", "api": "http://x/y.js"},
        ]
        for dirty in samples:
            with self.subTest(sample=dirty.get("key")):
                self.assertTrue(K.site_conflicts(dirty), "样本应被判为冲突")
                fixed = S.clean_site(dict(dirty))
                self.assertIsNotNone(fixed, "样本应能被修好而不是丢弃")
                self.assertEqual(K.site_conflicts(fixed), [],
                                 f"消毒后仍冲突：{fixed}")

    def test_url_rules_come_from_contract(self):
        """URL 判定必须读契约（曾因中文域名导致整份配置失败）。"""
        self.assertFalse(K.url_ok("https://深色壁纸.xxooo.cf/"), "中文域名应被拒")
        self.assertFalse(K.url_ok("http://127.0.0.1/a"), "localhost 应被拒")
        self.assertFalse(K.url_ok("./spider.jar"), "相对路径应被拒（配置文件内的引用除外）")
        self.assertTrue(K.url_ok("https://ok.com/a.json"))

    def test_text_rules_come_from_contract(self):
        self.assertTrue(K.text_conflicts(b"\xef\xbb\xbf{\"a\":1}" + b"\n"),
                        "带 BOM 应被报出")
        self.assertEqual(K.text_conflicts(b'{"a":1}' + b"\n"), [])


class TestQualityGate(unittest.TestCase):
    """门禁必须真的拦得住坏产物 —— 拦不住的门禁等于没有。"""

    BASE = {"sites": 1000, "live_channels": 5000}

    @staticmethod
    def _metrics(**kw):
        m = {"sites": 1000, "parses": 200, "live_channels": 5000,
             "live_groups": 30, "contract_conflicts": 0, "artifacts": {},
             "live_group_names": ["央视", "卫视"]}
        m.update(kw)
        return m

    def test_blocks_contract_conflict(self):
        ok, hard, _ = G.evaluate(
            self._metrics(contract_conflicts=1,
                          conflict_detail={"site": ["#0 x: categories 期望 list 实际 str"]}),
            self.BASE)
        self.assertFalse(ok, "契约冲突未拦住")
        self.assertTrue(any("契约冲突" in h for h in hard))

    def test_blocks_site_drop(self):
        ok, hard, _ = G.evaluate(self._metrics(sites=100), self.BASE)
        self.assertFalse(ok, "站点骤降未拦住")
        self.assertTrue(any("骤降" in h for h in hard))

    def test_blocks_live_channel_drop(self):
        ok, hard, _ = G.evaluate(self._metrics(live_channels=10), self.BASE)
        self.assertFalse(ok, "直播频道骤降未拦住")

    def test_blocks_missing_key_group(self):
        ok, hard, _ = G.evaluate(self._metrics(live_group_names=["卫视", "地方"]), self.BASE)
        self.assertFalse(ok, "关键分组缺失未拦住")
        self.assertTrue(any("央视" in h for h in hard))

    def test_passes_healthy_artifact(self):
        ok, hard, _ = G.evaluate(self._metrics(), self.BASE)
        self.assertTrue(ok, f"健康产物被误拦：{hard}")

    def test_absolute_floor_applies_without_baseline(self):
        ok, hard, _ = G.evaluate(self._metrics(sites=50), None)
        self.assertFalse(ok, "无基线时绝对下限未生效")

    def test_oversize_is_warning_not_blocker(self):
        """体积超标只警告 —— 2026-10-05 已证实体积不是解析失败主因。"""
        ok, _, soft = G.evaluate(self._metrics(artifacts={"tv-lite.json": 10_000_000}),
                                 self.BASE)
        self.assertTrue(ok, "体积超标不应拦截发布")
        self.assertTrue(any("超过契约上限" in w for w in soft))


class TestWatchdogLogic(unittest.TestCase):
    """巡检判定逻辑（网络 mock 掉，只测判定）。"""

    @staticmethod
    def _good_sites(n=320):
        return [{"key": f"k{i}", "name": f"K{i}", "type": 0,
                 "api": "http://ok.com/a"} for i in range(n)]

    @staticmethod
    def _live_body(nch=60):
        lines = ["央视,#genre#"] + [f"CCTV{i},http://a/{i}.m3u8" for i in range(nch)]
        return NL.join(lines).encode()

    def _fake(self, mapping, err="HTTP 404"):
        def _f(url, timeout=60, retries=2):
            for path, val in mapping.items():
                if url.endswith(path):
                    body, ct = val
                    return body, {"Content-Type": ct}, ""
            return None, {}, err
        return _f

    def _healthy_mapping(self):
        return {
            "/tv": (json.dumps({"sites": self._good_sites(), "parses": [],
                                "lives": []}).encode(), "application/json; charset=utf-8"),
            "/live": (self._live_body(), "text/plain; charset=utf-8"),
            "/spider.jar": (b"PK\x03\x04aaa", "application/java-archive"),
            "/status.json": (json.dumps({"generated_at": "2099-01-01T00:00:00Z"}).encode(),
                             "application/json"),
            "/lite": (b"{}", "application/json"),
            "/live-mini.txt": (b"{}", "application/json"),
        }

    def test_healthy_when_everything_is_good(self):
        with mock.patch.object(W, "fetch", self._fake(self._healthy_mapping())):
            rep = W.check("http://x", timeout=1)
        self.assertTrue(rep["healthy"], f"健康线上被误判：{rep['errors']}")

    def test_detects_contract_conflict(self):
        bad = json.dumps({"sites": [{"key": "k", "name": "K", "type": 0,
                                     "categories": "电影"}], "parses": [], "lives": []}).encode()
        m = self._healthy_mapping()
        m["/tv"] = (bad, "application/json")
        with mock.patch.object(W, "fetch", self._fake(m)):
            rep = W.check("http://x", timeout=1)
        self.assertFalse(rep["healthy"], "线上契约冲突未被发现")

    def test_detects_wrong_content_type_for_jar(self):
        """spider.jar 曾被以 image/png 返回，TVBox 内核会直接拒绝加载。"""
        m = self._healthy_mapping()
        m["/spider.jar"] = (b"\x89PNG", "image/png")
        with mock.patch.object(W, "fetch", self._fake(m)):
            rep = W.check("http://x", timeout=1)
        self.assertFalse(rep["healthy"], "jar 的错误 Content-Type 未被发现")
        self.assertTrue(any("java-archive" in e for e in rep["errors"]), rep["errors"])

    def test_detects_unreachable_required_paths(self):
        with mock.patch.object(W, "fetch", self._fake({})):
            rep = W.check("http://x", timeout=1)
        self.assertFalse(rep["healthy"])
        self.assertEqual(len(rep["errors"]), 3, "三个必需地址都应报错")

    def test_detects_site_count_below_floor(self):
        bad = json.dumps({"sites": self._good_sites(5), "parses": [], "lives": []}).encode()
        m = self._healthy_mapping()
        m["/tv"] = (bad, "application/json")
        with mock.patch.object(W, "fetch", self._fake(m)):
            rep = W.check("http://x", timeout=1)
        self.assertFalse(rep["healthy"], "站点数低于下限未被发现")


if __name__ == "__main__":
    unittest.main(verbosity=2)
