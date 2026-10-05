#!/usr/bin/env python3
"""
tv-hub 测试。标准库 unittest，无需pytest。

覆盖：
  1. sources.json 配置校验（含非法配置拒绝）
  2. TVBox 结构识别与校验（sites/parses/flags/spider）
  3. live.txt / live.m3u 解析（含真实上游的 `名称,#genre#` 写法）
  4. 去重逻辑（key 优先、URL 不同但内容相同也去重）
  5. 失败回退（保留上次成功结果、consecutive_failures 累加、status=stale）
  6. 全失败且无缓存 -> 必须拒绝产出空配置
  7. 实际产物 public/tv.json 可解析且结构合法
  8. 至少两个 TVBox 上游实际成功（读cache/_state.json 实证）

运行：python -m unittest discover -s tests -v
   或：python tests/test_tvhub.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import common as C  # noqa: E402
import merge as M  # noqa: E402
import build as B  # noqa: E402
import validate as V  # noqa: E402


class TestSourcesConfig(unittest.TestCase):
    def test_load_real_sources(self):
        cfg = C.load_sources()
        self.assertGreaterEqual(len(cfg["sources"]), 3)
        ids = [s["id"] for s in cfg["sources"]]
        self.assertEqual(len(ids), len(set(ids)), "source id 必须唯一")
        for s in cfg["sources"]:
            self.assertTrue(s["primary"].startswith("https://"), s["id"])
            self.assertIn(s["type"], C.VALID_TYPES)

    def test_reject_bad_config(self):
        bad_cases = [
            {"sources": "not-a-list"},
            {"sources": [{"id": "a", "name": "n", "type": "tvbox"}]},           # 缺 primary
            {"sources": [{"id": "a", "name": "n", "type": "xxx", "primary": "https://x"}]},  # 错 type
            {"sources": [{"id": "a", "name": "n", "type": "tvbox", "primary": "ftp://x"}]},   # 非 http
            {"sources": [{"id": "d", "name": "n", "type": "tvbox", "primary": "https://a"},
                         {"id": "d", "name": "m", "type": "tvbox", "primary": "https://b"}]},  # 重复 id
            {"sources": [{"id": "a", "name": "n", "type": "tvbox", "primary": "https://x",
                          "fallback": "https://y"}]},                              # fallback 非数组
        ]
        for i, bad in enumerate(bad_cases):
            with tempfile.TemporaryDirectory() as td:
                p = os.path.join(td, "sources.json")
                with open(p, "w", encoding="utf-8-sig") as f:
                    json.dump(bad, f, ensure_ascii=False)
                with self.assertRaises(ValueError, msg=f"case {i} 应被拒绝"):
                    C.load_sources(p)


class TestTvboxStructure(unittest.TestCase):
    def test_detect_shape(self):
        self.assertEqual(C.detect_shape({"sites": []}), "tvbox")
        self.assertEqual(C.detect_shape({"urls": []}), "subscription")
        self.assertEqual(C.detect_shape([1, 2]), "list")
        self.assertEqual(C.detect_shape({"a": 1}), "dict")

    def test_full_tvbox_ok(self):
        data = {"spider": "http://x/jar", "sites": [{"key": "a", "name": "A", "type": 1, "api": "http://a"}],
                "parses": [{"name": "p", "type": 1}], "flags": ["youku"], "lives": [], "doh": [], "rules": []}
        r = C.validate_tvbox(data)
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["counts"]["sites"], 1)
        self.assertEqual(r["counts"]["flags"], 1)
        self.assertEqual(r["counts"]["spider"], 1)

    def test_empty_sites_allowed_but_flagged(self):
        r = C.validate_tvbox({"sites": [], "lives": [{"name": "x", "url": "http://a"}]})
        self.assertTrue(r["ok"], "直播壳允许 sites 为空")
        self.assertTrue(any("sites 为空" in w for w in r["warnings"]))

    def test_min_sites_gate_rejects_empty(self):
        r = C.validate_tvbox({"sites": []}, min_sites=1)
        self.assertFalse(r["ok"])
        self.assertTrue(any("最低要求" in e for e in r["errors"]))

    def test_reject_wrong_field_type(self):
        # sites 不是数组 -> 不能通过
        r = C.validate_tvbox({"sites": {"a": 1}})
        self.assertFalse(r["ok"])
        self.assertTrue(any("不是数组" in e for e in r["errors"]))

    def test_reject_non_dict_site_element(self):
        r = C.validate_tvbox({"sites": ["just-a-string"]})
        self.assertFalse(r["ok"])
        self.assertTrue(any("非对象" in e for e in r["errors"]))

    def test_reject_bad_spider_type(self):
        r = C.validate_tvbox({"sites": [{"key": "a"}], "spider": {"x": 1}})
        self.assertFalse(r["ok"])
        self.assertTrue(any("spider" in e for e in r["errors"]))

    def test_non_tvbox_rejected(self):
        r = C.validate_payload({"type": "tvbox"}, json.dumps({"foo": "bar"}).encode())
        self.assertFalse(r["ok"])
        self.assertIn("不是 TVBox 结构", r["errors"][0])

    def test_broken_json_rejected(self):
        r = C.validate_payload({"type": "tvbox"}, b"{not json")
        self.assertFalse(r["ok"])
        self.assertIn("JSON 解析失败", r["errors"][0])


class TestLiveParsing(unittest.TestCase):
    def test_real_world_genre_style(self):
        """上游实测写法：分组是 `名称,#genre#`（# 不在行首）。"""
        text = ("🕘️更新时间,#genre#\n"
                "2026-10-04 03:16:34,http://a.com/1.m3u8\n"
                "📺央视频道,#genre#\n"
                "CCTV-10,http://a.com/cctv10.m3u8\n"
                "CCTV-10,http://b.com/cctv10.m3u8\n")
        r = C.validate_live_txt(text)
        self.assertTrue(r["ok"])
        self.assertEqual(r["counts"]["channels"], 3)
        self.assertEqual(r["counts"]["groups"], 2)
        self.assertEqual({c["group"] for c in r["channels"]},
                         {"央视", "未分组"})   # 时间戳行归入未分组，不产生时间戳组

    def test_hash_at_line_start_genre(self):
        r = C.validate_live_txt("#央视,#genre#\nCCTV1,http://a/1.m3u8\n")
        self.assertTrue(r["ok"])
        self.assertEqual(r["channels"][0]["group"], "央视")

    def test_skip_invalid_rows(self):
        text = "bad_line_without_url\n\nX,ftp://bad\nY,http://ok/1.m3u8\n"
        r = C.validate_live_txt(text)
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["channels"]), 1)
        self.assertTrue(len(r["warnings"]) >= 2)

    def test_empty_live_rejected(self):
        self.assertFalse(C.validate_live_txt("")["ok"])

    def test_m3u_parse(self):
        text = ('#EXTM3U\n'
                '#EXTINF:-1 group-title="央视",CCTV-1\nhttp://a/1.m3u8\n'
                '#EXTINF:-1 tvg-id="cctv1" group-title="卫视",湖南卫视\nhttp://b/2.m3u8\n')
        r = C.validate_live_m3u(text)
        self.assertTrue(r["ok"])
        self.assertEqual(r["counts"]["channels"], 2)
        self.assertEqual(r["counts"]["groups"], 2)
        self.assertEqual(r["channels"][1]["tvg_id"], "cctv1")

    def test_group_normalisation(self):
        cases = {"📺央视频道,": "央视", "📡卫视频道,": "卫视", "☘️浙江频道,": "地方-浙江",
                 "港·澳·台": "港台", "央视": "央视", "轮播": "轮播", "新疆频道": "地方-新疆"}
        for raw, want in cases.items():
            self.assertEqual(C.norm_group(raw), want, f"{raw} 应归一化为 {want}")


class TestDedup(unittest.TestCase):
    def test_same_key_dedup(self):
        dd = M.Deduper()
        a = {"key": "k1", "name": "A", "type": 1, "api": "http://a"}
        b = {"key": "k1", "name": "A改名", "type": 1, "api": "http://b"}   #同 key，不同 URL
        self.assertTrue(dd.accept(M.site_identity(a), "s1"))
        self.assertFalse(dd.accept(M.site_identity(b), "s2"))

    def test_identical_content_different_url_source(self):
        """两个来源 URL 不同但站点内容完全相同 -> 必须去重。"""
        site = {"key": "", "name": "同站", "type": 1, "api": "http://same/api"}
        dd = M.Deduper()
        self.assertTrue(dd.accept(M.site_identity(dict(site)), "srcA"))
        self.assertFalse(dd.accept(M.site_identity(dict(site)), "srcB"))

    def test_fallback_to_name_type(self):
        dd = M.Deduper()
        x = {"name": "X站", "type": 1, "api": ""}
        y = {"name": "x站 ", "type": 1, "api": ""}   # 名字大小写/空格差异
        self.assertTrue(dd.accept(M.site_identity(x), "s1"))
        self.assertFalse(dd.accept(M.site_identity(y), "s2"))

    def test_distinct_sites_kept(self):
        dd = M.Deduper()
        self.assertTrue(dd.accept(M.site_identity({"key": "a", "name": "A"}), "s1"))
        self.assertTrue(dd.accept(M.site_identity({"key": "b", "name": "B"}), "s1"))

    def test_normalize_site_type_string(self):
        self.assertEqual(M.normalize_site({"type": "3"})["type"], 3)
        self.assertEqual(M.normalize_site({"type": "junk"})["type"], 0)
        self.assertEqual(M.normalize_site({"type": 1})["type"], 1)

    def test_normalize_site_ext(self):
        s = M.normalize_site({"ext": [{"name": "A", "url": "http://a"}]})
        self.assertEqual(s["ext"], {"A": "http://a"})
        self.assertNotIn("ext", M.normalize_site({"ext": None}))
        self.assertNotIn("ext", M.normalize_site({"ext": {}}))
        self.assertNotIn("ext", M.normalize_site({"ext": ""}))

    def test_flags_are_strings(self):
        dd = M.Deduper()
        stats: dict = {}
        out = M.merge_flags([("s1", "youku"), ("s1", "优酷"), ("s2", "YOUKU"), ("s2", "")], dd, stats)
        self.assertIn("youku", out)
        self.assertIn("优酷", out)
        self.assertEqual(len(out), 2, "大小写相同的 flag 应去重")
        self.assertEqual(stats.get("flags_invalid"), 1)

    def test_classify_lives_rescues_sites(self):
        """上游把站点对象误放进 lives -> 必须分流成site，不能原样进 lives。"""
        stats: dict = {}
        items = [("s1", {"name": "A", "api": "csp_A", "jar": "http://j"}),
                 ("s1", {"name": "B", "url": "http://b.txt"}),
                 ("s1", {"name": "C", "url": "", "playerType": 1})]
        good, rescued = M.classify_lives(items, stats)
        self.assertEqual(len(good), 1)
        self.assertEqual(len(rescued), 1)
        self.assertEqual(rescued[0]["name"], "A")
        self.assertEqual(stats["lives_rescued_as_site"], 1)


class TestFetchFallback(unittest.TestCase):
    """失败回退实证：抓不到时保留上次成功结果，不清空。

    注意：必须用unittest.mock.patch 临时改common 模块级路径，
    直接赋值会在 tearDown 之外污染其它测试（曾导致 sources.json 被读到临时配置）。
    """

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="tvhub_test_")
        self.cache = os.path.join(self.td, "cache")
        self.pub = os.path.join(self.td, "public")
        self.cfgdir = os.path.join(self.td, "config")
        for d in (self.cache, self.pub, self.cfgdir):
            os.makedirs(d, exist_ok=True)

        # 硬性隔离：真实 config/sources.json 与 cache/ 一旦被本测试写入即属事故，
        # 直接在写之前拦下（曾真实发生过：sources.json 被覆盖成测试用单源配置）。
        real_cfg = os.path.join(ROOT, "config", "sources.json")
        real_cfg_dir = os.path.join(ROOT, "config")
        real_cache = os.path.join(ROOT, "cache")
        real_pub = os.path.join(ROOT, "public")

        def _guard(path: str) -> None:
            ap_ = os.path.abspath(path)
            for guarded in (real_cfg,):
                if ap_ == os.path.abspath(guarded):
                    raise AssertionError(f"测试试图写真实配置文件：{path}")
            for guarded_dir in (real_cfg_dir, real_cache, real_pub):
                if ap_.startswith(os.path.abspath(guarded_dir) + os.sep):
                    raise AssertionError(f"测试试图写真实项目目录：{path}")

        _orig_write_json = C.write_json
        _orig_write_text = open

        def _safe_write_json(path, obj):
            _guard(path)
            return _orig_write_json(path, obj)

        self._p = [
            mock.patch.object(C, "write_json", _safe_write_json),
            mock.patch.object(C, "CACHE_DIR", self.cache),
            mock.patch.object(C, "PUBLIC_DIR", self.pub),
            mock.patch.object(C, "ROOT", self.td),
            mock.patch.object(C, "CONFIG_DIR", self.cfgdir),
            mock.patch.object(C, "SOURCES_FILE", os.path.join(self.cfgdir, "sources.json")),
            mock.patch.object(C, "STATE_FILE", os.path.join(self.cache, "_state.json")),
            mock.patch.object(C, "cache_path",
                              lambda sid: os.path.join(self.cache, f"{sid}.json")),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in reversed(self._p):
            p.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def _src(self, sid="s1"):
        return {"id": sid, "name": "测试源", "type": "tvbox", "repo": "r/r",
                "primary": "https://invalid.invalid/a.json",
                "fallback": ["https://also.invalid/b.json"], "enabled": True}

    def test_failure_keeps_previous_success(self):
        good = json.dumps({"sites": [{"key": "a", "name": "A", "type": 1, "api": "http://a"}]}).encode()
        os.makedirs(C.CACHE_DIR, exist_ok=True)
        with open(C.cache_path("s1"), "wb") as f:
            f.write(good)

        st = C.load_state()
        st["sources"]["s1"] = {"status": "ok", "last_success": "2026-10-01T00:00:00Z",
                               "consecutive_failures": 0}
        C.save_state(st)

        import fetch as F
        dflt = {"timeout": 3, "retries": 0, "retry_backoff_seconds": 0, "user_agent": "test"}
        rec = F.fetch_one(self._src(), dflt)

        self.assertEqual(rec["status"], "stale", "抓取失败但有缓存 -> stale")
        self.assertTrue(rec["from_cache"])
        self.assertEqual(rec["consecutive_failures"], 1)
        self.assertEqual(rec["last_success"], "2026-10-01T00:00:00Z", "last_success 必须保留")
        self.assertIsNotNone(rec["error"])
        # 缓存文件内容必须未被破坏
        with open(C.cache_path("s1"), "rb") as f:
            self.assertEqual(f.read(), good)

    def test_consecutive_failures_accumulate(self):
        os.makedirs(C.CACHE_DIR, exist_ok=True)
        with open(C.cache_path("s1"), "wb") as f:
            f.write(b'{"sites":[{"key":"a","name":"A"}]}')
        import fetch as F
        dflt = {"timeout": 3, "retries": 0, "retry_backoff_seconds": 0, "user_agent": "test"}
        st = C.load_state()
        for expected in (1, 2, 3):
            st["sources"]["s1"] = {"status": "stale", "consecutive_failures": expected - 1,
                                   "last_success": "2026-10-01T00:00:00Z"}
            C.save_state(st)
            rec = F.fetch_one(self._src(), dflt)
            self.assertEqual(rec["consecutive_failures"], expected)

    def test_no_cache_marks_failed(self):
        import fetch as F
        dflt = {"timeout": 3, "retries": 0, "retry_backoff_seconds": 0, "user_agent": "test"}
        rec = F.fetch_one(self._src(), dflt)
        self.assertEqual(rec["status"], "failed")
        self.assertFalse(rec["from_cache"])
        self.assertEqual(rec["consecutive_failures"], 1)

    def test_build_refuses_when_all_sources_dead(self):
        """全部上游失败且无历史缓存 -> 构建必须失败，不能产出空配置。"""
        os.makedirs(C.CONFIG_DIR, exist_ok=True)
        cfg = {"version": 1, "sources": [{"id": "dead", "name": "死源", "type": "tvbox",
                                          "primary": "https://dead.invalid/x.json", "enabled": True}]}
        with open(C.SOURCES_FILE, "w", encoding="utf-8-sig") as f:
            json.dump(cfg, f, ensure_ascii=False)
        import fetch as F
        dflt = {"timeout": 3, "retries": 0, "retry_backoff_seconds": 0, "user_agent": "test"}
        state = C.load_state()
        state["sources"]["dead"] = F.fetch_one(cfg["sources"][0], dflt)
        C.save_state(state)

        rc = _run_build()
        self.assertNotEqual(rc, 0, "无任何可用上游时build 必须非零退出")
        self.assertFalse(os.path.exists(os.path.join(C.PUBLIC_DIR, "tv.json")),
                         "绝不能生成看起来正常但为空的 tv.json")

    def test_status_json_contains_required_fields(self):
        rec = {"id": "s1", "name": "n", "type": "tvbox", "repo": "r", "path": "p",
               "status": "stale", "last_attempt": "2026-10-04T00:00:00Z",
               "last_success": "2026-10-01T00:00:00Z", "consecutive_failures": 2,
               "used_url": "https://x/y", "http_status": 500, "bytes": 10,
               "sha256": "deadbeef", "counts": {"sites": 3}, "from_cache": True,
               "error": "boom", "warnings": []}
        cfg = {"sources": [self._src()]}
        st = C.load_state()
        st["sources"]["s1"] = rec
        status = B.build_status(cfg, st, {"unique_sites": 3})
        r = status["sources"][0]
        for f in ("status", "last_attempt", "last_success", "consecutive_failures",
                  "used_url", "counts", "error"):
            self.assertIn(f, r)
        self.assertEqual(r["consecutive_failures"], 2)
        self.assertEqual(status["summary"]["stale"], 1)


def _run_build() -> int:
    import sys as _s
    old = _s.argv
    _s.argv = ["build.py"]
    try:
        return B.main()
    finally:
        _s.argv = old


class TestWorkflowLint(unittest.TestCase):
    """workflow 结构校验 —— 防止再次出现「GitHub 接受文件但 run 秒级失败、jobs=[]」。

    真实事故：`- name: 汇总结果` 被误写成顶格（0缩进），
    GitHub 创建了 run #1（conclusion=failure）但 jobs=[]、created_at==updated_at，
    runner 从未被分配，网页日志里看不到任何步骤输出。
    """

    WF = os.path.join(ROOT, ".github", "workflows", "update.yml")

    def test_real_workflow_passes(self):
        import workflow_lint
        errs = workflow_lint.lint(self.WF)
        self.assertEqual(errs, [], f"workflow 结构错误：{errs}")

    def test_catches_top_level_step_name(self):
        """真实事故的回归测试：顶格 - name 必须被抓出。"""
        import workflow_lint
        src = open(self.WF, encoding="utf-8-sig").read()
        broken = src.replace("      - name: 汇总结果", "- name: 汇总结果")
        self.assertNotEqual(broken, src, "测试前提失效：未找到汇总结果锚点")
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "broken.yml")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(broken)
            errs = workflow_lint.lint(p)
        self.assertTrue(errs, "顶格 - name 必须被 lint 抓出")
        self.assertTrue(any("缩进不一致" in e for e in errs), errs)

    def test_catches_missing_quotes_on_on(self):
        import workflow_lint
        src = open(self.WF, encoding="utf-8-sig").read()
        broken = src.replace('"on":', "on:")
        self.assertNotEqual(broken, src, "测试前提失效：未找到 on 锚点")
        self.assertNotEqual(broken, src)
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "broken.yml")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(broken)
            errs = workflow_lint.lint(p)
        self.assertTrue(any("引号" in e for e in errs), errs)

    def test_catches_bad_cron(self):
        import workflow_lint
        src = open(self.WF, encoding="utf-8-sig").read()
        broken = src.replace('cron: "5 2 * * *"', 'cron: "5 2 * *"')
        self.assertNotEqual(broken, src, "测试前提失效：未找到 cron 锚点")
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "broken.yml")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(broken)
            errs = workflow_lint.lint(p)
        self.assertTrue(any("cron" in e for e in errs), errs)

    def test_catches_tab_indent(self):
        import workflow_lint
        src = open(self.WF, encoding="utf-8-sig").read()
        broken = src.replace("      - name: Checkout", "\t- name: Checkout")
        self.assertNotEqual(broken, src)
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "broken.yml")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(broken)
            errs = workflow_lint.lint(p)
        self.assertTrue(any("TAB" in e for e in errs), errs)

    def test_cron_times_are_shanghai_1005_and_2205(self):
        """cron 必须对应北京时间 10:05 与 22:05 = UTC 02:05 与 14:05。
        cron 五段顺序是「分 时 日 月 周」，所以 10:05 → `5 2`，22:05 → `5 14`。
        刻意避开整点（`2 2` / `2 14` 已废弃），减少 GitHub 整点高峰排队延迟。"""
        import re
        src = open(self.WF, encoding="utf-8-sig").read()
        crons = sorted(re.findall(r'cron:\s*"([^"]+)"', src))
        self.assertEqual(crons, ["5 14 * * *", "5 2 * * *"],
                         f"cron 与北京 10:05/22:05 不符：{crons}（应为 5 2 与 5 14）")

    def test_cron_avoids_on_the_hour(self):
        """回归测试：曾写成 `2 2`/`2 14`（注释说整点、实际第2 分钟运行），
        已改为 `5 2`/`5 14`。本用例防止回退。"""
        import re
        src = open(self.WF, encoding="utf-8-sig").read()
        crons = re.findall(r'cron:\s*"([^"]+)"', src)
        self.assertTrue(crons, "未找到 cron 配置")
        for c in crons:
            minute = int(c.split()[0])
            self.assertNotEqual(
                minute, 0,
                f"cron `{c}` 在整点触发，应偏移到第 5 分钟以避开高峰")

    def test_heredoc_terminators_aligned(self):
        import workflow_lint
        # lint 内部对未闭合/错位 heredoc 会报错；此处确保真实文件确实有 heredoc 且被正确识别
        src = open(self.WF, encoding="utf-8-sig").read()
        self.assertIn("<<'PY'", src)
        self.assertEqual(workflow_lint.lint(self.WF), [])


class TestBomConsistency(unittest.TestCase):
    """回归测试：产物一律带 UTF-8 BOM，因此所有读取处必须用 utf-8-sig。

    真实事故：引入 BOM 后，update.yml 的「汇总结果」步骤用 encoding="utf-8"
    读 status.json → JSONDecodeError: Unexpected UTF-8 BOM → run#3 在第 8 步失败。
    本类确保同类问题不会再发生。
    """

    PROD_DIR = os.path.join(ROOT, "public")

    def test_all_json_artifacts_have_no_bom(self):
        """所有 json 产物不得带 BOM —— 其实而手机端 Android org.json 会把 BOM 当非法字符，
        解析配置直接失败。"""
        if not os.path.isdir(self.PROD_DIR):
            self.skipTest("尚未构建")
        n = 0
        for fn in sorted(os.listdir(self.PROD_DIR)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(self.PROD_DIR, fn), "rb") as f:
                head = f.read(3)
            self.assertNotEqual(head, b"\xef\xbb\xbf", f"{fn} 仍带 UTF-8 BOM，客户端会解析失败")
            with open(os.path.join(self.PROD_DIR, fn), "rb") as f:
                self.assertEqual(f.read(1), b"{", f"{fn} 不是以 JSON 对象开头")
            n += 1
        if n == 0:
            self.skipTest("无json 产物")

    def test_state_file_roundtrip_with_bom(self):
        """状态文件读写闭环：写入带 BOM → 读取必须成功。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            orig = C.STATE_FILE
            try:
                C.STATE_FILE = os.path.join(td, "_state.json")
                C.save_state({"version": 1, "sources": {"x": {"status": "ok"}}, "runs": []})
                with open(C.STATE_FILE, "rb") as f:
                    self.assertNotEqual(f.read(3), b"\xef\xbb\xbf", "状态文件不应带 BOM")
                st = C.load_state()          # 不得抛异常
                self.assertIn("x", st["sources"])
            finally:
                C.STATE_FILE = orig

    def test_no_bom_written_in_code(self):
        """回归：产物必须不带 BOM（否则 Android org.json 解析失败）。"""
        bad: list[str] = []
        targets = ("scripts/build.py", "scripts/validate.py", "scripts/merge.py",
                   "scripts/common.py", ".github/workflows/update.yml")
        for rel in targets:
            fp = os.path.join(ROOT, *rel.split("/"))
            if not os.path.exists(fp):
                continue
            with open(fp, encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if "utf-8-sig" in line and "write" in line:
                        bad.append(f"{rel}:{i}: {line.strip()[:78]}")
        self.assertEqual(bad, [], "以下位置仍在写入出带 BOM 的文件：" + chr(10).join(bad))

    def _load(self, name):
        p = os.path.join(ROOT, "public", name)
        if not os.path.exists(p):
            self.skipTest("尚未构建，先跑 python scripts/build.py")
        with open(p, "r", encoding="utf-8-sig") as f:
            return json.load(f)

    def test_subscription_is_urls_list(self):
        """必须是 App 认识的 {urls:[{name,url}]}，否则填了也没用。"""
        d = self._load("subscriptions.json")
        self.assertIsInstance(d.get("urls"), list)
        self.assertGreater(len(d["urls"]), 0)
        for u in d["urls"]:
            self.assertIn("name", u)
            self.assertTrue(u["url"].startswith("https://"))

    def test_base_url_rejects_unrendered_github_expr(self):
        """回归：workflow 曾传 `${{ vars.X || '${{ y }}' }}`，GitHub 不二次展开，
        模板文本被原样写入订阅 → 所有线路 URL 失效。
        验证 base_url() 会识别并回落。"""
        import merge as M
        for bad in ("${{ github.repository }}",
                    "https://raw.githubusercontent.com/${{ github.repository }}/${{ github.ref_name }}",
                    "${{ vars.TVHUB_BASE_URL }}",
                    "ftp://bad", "", "   "):
            with mock.patch.dict(os.environ, {"TVHUB_BASE_URL": bad}):
                self.assertEqual(M.base_url(), M.DEFAULT_BASE_URL,
                                 f"非法基址 {bad!r} 应回落到默认值")
        good = "https://example.com/base/"
        with mock.patch.dict(os.environ, {"TVHUB_BASE_URL": good}):
            self.assertEqual(M.base_url(), "https://example.com/base")

    def test_subscription_urls_have_no_template_vars(self):
        """订阅里的每条 URL 都必须是真实可用的地址，不含 GitHub 表达式。"""
        d = self._load("subscriptions.json")
        blob = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("${{", blob, "订阅文件里出现了未渲染的 GitHub 表达式")
        for u in d["urls"]:
            self.assertTrue(u["url"].startswith("https://"), u["url"])
            self.assertNotIn("${{", u["url"])

    def test_first_entry_is_aggregate(self):
        """urls[0] 必须是体积最小的那一档。

        源码依据（FongMi/TV VodConfig.parseDepot）：
            load(this.config = configs.get(0));
        parseDepot 只自动加载 urls[0]，其余条目仅供 App 内手动切换。
        若 urls[0] 是大文件，App 启动即解析失败（实测讴歌 6.0.9.3：
        2.8MB 的 tv.json 失败，136KB 的 tv-lite 可用）。
        """
        d = self._load("subscriptions.json")
        first = d["urls"][0]
        self.assertTrue(first["url"].endswith("tv-lite.json"),
                        f"urls[0] 必须是 tv-lite.json（App 只自动加载它），实际 {first['url']}")

    def test_all_entries_reachable_and_valid(self):
        """订阅清单里每一条都必须真实可打开 —— 这是用户切换时会踩的地方。"""
        import validate as V
        d = self._load("subscriptions.json")
        for u in d["urls"]:
            url = u["url"]
            # URL 形如 https://host/tv.json 或 https://host/profiles/xxx.json
            m = re.match(r"^https://[^/]+/(.+)$", url)
            self.assertTrue(m, f"URL 格式异常: {url}")
            rel = m.group(1)
            p = os.path.join(ROOT, "public", *rel.split("/"))
            self.assertTrue(os.path.exists(p), f"订阅条目无对应文件: {url} -> {p}")
            with open(p, encoding="utf-8-sig") as f:
                data = json.load(f)
            if "urls" in data:          # 多仓订阅原样转发，跳过 TVBox 校验
                self.assertTrue(data["urls"])
                continue
            r = V.validate_tv_output(data)
            self.assertTrue(r["ok"], f"{url} 不可用: {r['errors'][:3]}")
            self.assertGreater(r["counts"]["sites"], 0)

    def test_tiers_key_order_matches_known_good(self):
        """分档配置的键序必须与实测可用的 ysc_single_agg 一致：
        spider → wallpaper → logo → ... → sites → lives → parses → flags
        （sites 不能排在第 2 位）。
        """
        ref = self._load("profiles/ysc_single_agg.json")
        ref_keys = list(ref.keys())
        for name in ("tv-lite.json", "tv-standard.json"):
            p = os.path.join(ROOT, "public", name)
            if not os.path.exists(p):
                self.skipTest("尚未生成分档")
            with open(p, encoding="utf-8-sig") as f:
                d = json.load(f)
            ks = list(d.keys())
            # 分档没有 warningText，跳过比较缺失的键
            common = [k for k in ref_keys if k in ks]
            self.assertEqual([k for k in ks if k in common], common,
                             f"{name} 键序与已知可用配置不一致：{ks}")

    def test_lite_tier_smaller_than_full(self):
        """分档必须显著小于全量，否则 urls[0] 仍会超客户端上限。"""
        lite = os.path.join(ROOT, "public", "tv-lite.json")
        full = os.path.join(ROOT, "public", "tv.json")
        if not (os.path.exists(lite) and os.path.exists(full)):
            self.skipTest("尚未构建")
        self.assertLess(os.path.getsize(lite), os.path.getsize(full) / 3,
                        "轻量版体积应远小于全量版")

    def test_profiles_dir_has_no_empty_config(self):
        """单仓配置不能是空的 —— 复用主配置的 Deduper 会导致全站被去重掉。"""
        import validate as V
        pdir = os.path.join(ROOT, "public", "profiles")
        if not os.path.isdir(pdir):
            self.skipTest("尚未生成 profiles/")
        n = 0
        for fn in os.listdir(pdir):
            if not fn.endswith(".json"):
                continue
            n += 1
            with open(os.path.join(pdir, fn), encoding="utf-8-sig") as f:
                d = json.load(f)
            if "urls" in d:
                continue
            r = V.validate_tv_output(d)
            self.assertTrue(r["ok"], f"{fn} 不可用: {r['errors'][:3]}")
            # 站点数下限。ysc_single_agg 上游有 150+ 个 type=3 但缺 api 的残缺站点，
        # 按 TVBox 契约（type 1/3 必须有 api）必须剔除，否则内核抛异常 →
            # 整份配置「解析配置失败」。数量下降是修复的预期结果，不是回归。
            self.assertGreater(len(d.get("sites") or []), 10,
                               f"{fn} 站点数为 0，疑似被跨源 Deduper 误去重")
            # 契约本身由 validate_tv_output 断言（每条 type 1/3 都必须有 api）
        if n == 0:
            self.skipTest("profiles/ 为空")

    def test_base_url_is_custom_domain(self):
        """订阅地址应指向自定义域（Cloudflare），不是 raw。"""
        import merge as M
        self.assertIn("dpdns.org", M.base_url())


class TestLiveConfig(unittest.TestCase):
    """直播配置：与点播同一套适配逻辑（清洗 / 体积分档 / 键序 / BOM）。

    源码依据（FongMi/TV LiveConfig.initLive）：
        setLives(Live.objectFrom(e, spider).distinct()...)
        Collectors.toMap(Live::getName, ...)   // name 空/重复 → 抛异常
    """

    PUB = os.path.join(ROOT, "public")

    def _load(self, name):
        p = os.path.join(self.PUB, name)
        if not os.path.exists(p):
            self.skipTest(f"尚未生成 {name}")
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)

    def test_live_sub_urls0_is_smallest(self):
        """直播订阅 urls[0] 必须是体积最小的一档（App 只自动加载它）。"""
        d = self._load("live-sub.txt")
        self.assertTrue(d["urls"])
        first = d["urls"][0]["url"]
        self.assertTrue(first.endswith("live-mini.txt"),
                        f"urls[0] 应为 live-mini.txt，实际 {first}")

    def test_live_mini_under_client_limit(self):
        """最小档必须落在客户端可接受体积内（实测阈值 ~100KB）。"""
        p = os.path.join(self.PUB, "live-mini.txt")
        if not os.path.exists(p):
            self.skipTest("尚未生成 live-mini.txt")
        self.assertLess(os.path.getsize(p), 100_000,
                        "live-mini 体积必须 < 100KB，否则老客户端仍会解析失败")

    def test_no_unusable_live_urls(self):
        """不得含相对路径或 localhost 的直播源（客户端无法使用）。"""
        d = self._load("live.json")
        bad_rel = [x["name"] for x in d["lives"]
                   if x.get("url") and not str(x["url"]).startswith(("http://", "https://"))]
        bad_local = [x["name"] for x in d["lives"]
                     if str(x.get("url", "")).startswith(("http://127.0.0.1", "http://localhost"))]
        self.assertEqual(bad_rel, [], f"含相对路径直播源：{bad_rel[:5]}")
        self.assertEqual(bad_local, [], f"含 localhost 直播源：{bad_local[:5]}")

    def test_live_names_unique_and_nonempty(self):
        """Live.getName() 为空或重复会让 toMap 抛异常，整份配置加载失败。"""
        for f in ("live.json", "live-mini.txt", "live-lite.txt",
                  "live-standard.txt", "live-full.txt"):
            d = self._load(f)
            names = [str(x.get("name") or "") for x in d["lives"]]
            self.assertTrue(all(n.strip() for n in names), f"{f} 存在空 name")
            self.assertEqual(len(names), len(set(names)), f"{f} 存在重复 name")

    def test_backup_urls_capped(self):
        """备源必须收敛（实测卫视平均 20.4 个/频道，体积爆掉）。"""
        for f in ("live-mini.txt", "live-lite.txt", "live-standard.txt", "live-full.txt"):
            d = self._load(f)
            cap = 2 if f == "live-mini.txt" else 3
            for g in d["lives"]:
                for c in (g.get("channels") or []):
                    self.assertLessEqual(len(c.get("urls") or []), cap,
                                         f"{f} 的 {c.get('name')} 备源数超上限 {cap}")

    def test_live_tiers_have_no_bom_and_key_order(self):
        """直播分档不带 BOM，且 lives 不应排在第 2 位（对齐可用配置键序）。"""
        for f in ("live-mini.txt", "live-lite.txt", "live-standard.txt", "live-full.txt"):
            p = os.path.join(self.PUB, f)
            if not os.path.exists(p):
                continue
            with open(p, "rb") as fh:
                self.assertNotEqual(fh.read(3), b"\xef\xbb\xbf", f"{f} 仍带 BOM")
            d = self._load(f)
            self.assertIn("lives", d)
            self.assertNotEqual(list(d.keys())[1] if len(d) > 1 else "", "lives",
                                f"{f} 的 lives 排在第 2 位")

    def test_clean_live_entries_filters_bad(self):
        import merge as M
        # 分组名必须落在 merge.TV_GROUPS 白名单（央视/卫视/港台/地方-*）内，
        # 否则会被 keep_group_name 提前剔除，测不到后面三类过滤。
        bad = [
            {"name": "地方-相对", "url": "./lives/a.txt"},
            {"name": "地方-本地", "url": "http://127.0.0.1:9978/a.txt"},
            {"name": "", "url": "http://x/a.txt"},
            {"name": "央视", "url": "http://x/a.txt"},
            {"name": "卫视", "channels": [{"name": "C1", "urls": ["http://x/1"]}]},
            {"name": "轮播", "url": "http://x/a.txt"},
        ]
        out, st = M.clean_live_entries(bad)
        names = [x["name"] for x in out]
        self.assertEqual(names, ["央视", "卫视"], f"清洗结果不符：{names}")
        self.assertEqual(st["relative"], 1)
        self.assertEqual(st["localhost"], 1)
        self.assertEqual(st["noname"], 1)
        # 非电视台分组（轮播/其他等）应被白名单剔除
        self.assertEqual(st["non_tv_group"], 1)


class TestUrlScrubbing(unittest.TestCase):
    """回归测试：配置里不得存在客户端无法解析的 URL。

    真实事故（老板实测「全部无法解析 + 加载很慢」）：
      wallpaper = https://深色壁纸.xxooo.cf/   ← 中文域名
    Android 的 OkHttp 用 java.net.URI 解析 URL，非 ASCII 直接抛异常，
    客户端随即反复重试 → 表现为「一直转圈 / 加载很慢」。
    Python urlopen 走同一套 latin-1 规则，可精确复现该判定。

    实测脏数据规模：ysc_single_agg 16 处、tv.json 113 处。
    """

    def test_is_valid_remote_url(self):
        self.assertFalse(C.url_encodable("https://深色壁纸.xxooo.cf/"))
        self.assertTrue(C.url_encodable("https://xn--dkw0c.v.nxog.top/tv"))
        self.assertFalse(C.is_valid_remote_url("./lives/a.txt"))
        self.assertFalse(C.is_valid_remote_url("http://127.0.0.1:9978/a.txt"))
        self.assertFalse(C.is_valid_remote_url("http://localhost/a"))
        self.assertTrue(C.is_valid_remote_url("https://a.com/x.json"))

    def test_scrub_urls_removes_bad(self):
        src = {"wallpaper": "https://深色壁纸.xxooo.cf/",
               "logo": "http://ok.com/a.gif",
               "sites": [{"key": "k", "type": 1, "api": "http://iyiwang.com/花姐"},
                         {"key": "k2", "type": 1, "api": "https://ok.com/api"},
                         {"key": "k3", "type": 3, "api": "csp_X",
                          "jar": "https://jar.com/中文名.jar"}],
               "lives": [{"name": "L", "url": "https://ok.com/中文台.m3u8"},
                         {"name": "L2", "url": "https://ok.com/live"}]}
        out = C.scrub_urls(src)
        # 非 ASCII 的标量 URL 字段直接丢弃
        self.assertNotIn("wallpaper", out)
        self.assertIn("logo", out)
        # scrub_urls 只清 URL、不删站点（站点去留由 merge 层判断）：
        #   k 的 api 被剔除但站点仍在（type=0 无需 api）；k2 的 api 合法保留；k3 正常
        self.assertEqual([s["key"] for s in out["sites"]], ["k", "k2", "k3"])
        # ★ 行为变更（2026-10-05，见 LESSONS.md）：
        #   「ASCII 域名 + 未编码中文路径」不再删除，而是自动 percent-encode 保留。
        #   原因：上游 200+ 个源的 ext 写作 `http://101.34.67.237/js/秋霞.js`，
        #   一刀切删除让它们全变空壳（用户误以为「要会员」）。
        #   编码后是标准 URL 可被客户端解析；不编码则整份配置加载失败。
        #   **中文域名仍然必须剔除**（见上面 wallpaper 的断言）。
        self.assertEqual(out["sites"][0]["api"], "http://iyiwang.com/%E8%8A%B1%E5%A7%90")
        self.assertTrue(C.url_encodable(out["sites"][0]["api"]))
        self.assertIn("jar", out["sites"][2])           # 中文 jar 现在保留（已编码）
        self.assertTrue(C.url_encodable(out["sites"][2]["jar"]))
        # list 里的 dict 元素同理
        self.assertEqual([l["name"] for l in out["lives"]], ["L", "L2"])
        self.assertEqual(out["lives"][0]["url"], "https://ok.com/%E4%B8%AD%E6%96%87%E5%8F%B0.m3u8")
        self.assertTrue(C.url_encodable(out["lives"][0]["url"]))

    def test_chinese_hostname_still_rejected(self):
        """中文**域名**必须仍然被剔除 —— percent 编码路径救不了它，
        编码域名等于换了个域名，且部分客户端不做 punycode 转换。"""
        self.assertFalse(C.is_usable_url("https://深色壁纸.xxooo.cf/tv"))
        self.assertFalse(C.is_valid_remote_url(C.norm_url("https://深色壁纸.xxooo.cf/tv")))
        # 而「ASCII 域名 + 中文路径」编码后必须可用
        self.assertTrue(C.is_usable_url(C.norm_url("https://ok.com/中文台.m3u8")))

    def test_relative_path_allowed(self):
        """`./` 同源相对路径必须放行 —— 上游 hebi 的 spider/ext 全靠它，
        一刀切删除曾是 307 个源变空壳的根因。"""
        self.assertTrue(C.is_usable_url("./deps/localized/abc.js"))
        self.assertTrue(C.is_usable_url("./spider.jar"))
        self.assertTrue(C.is_usable_url(C.norm_url("./deps/auto/x/EMO蓝光[V2].js")))
        # 防目录逃逸 / 反斜杠
        self.assertFalse(C.is_usable_url("./../etc/passwd"))
        self.assertFalse(C.is_usable_url("./deps\\evil.js"))
        # 单纯相对路径不是远程地址（旧函数语义保持不变）
        self.assertFalse(C.is_valid_remote_url("./lives/a.txt"))

    def test_no_unencodable_url_in_any_artifact(self):
        """所有产物中不得存在无法编码的 URL。"""
        bad: list[str] = []
        pub = os.path.join(ROOT, "public")
        if not os.path.isdir(pub):
            self.skipTest("尚未构建")

        def walk(obj, path, f):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    walk(v, f"{path}.{k}", f)
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    walk(v, f"{path}[{i}]", f)
            elif isinstance(obj, str) and obj.startswith(("http://", "https://")):
                if not C.url_encodable(obj):
                    bad.append(f"{f}{path} = {obj[:60]}")

        for dirpath, _, files in os.walk(pub):
            for fn in files:
                if not fn.endswith((".json", ".txt", ".webp")):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    with open(p, encoding="utf-8-sig") as f:
                        walk(json.load(f), "", os.path.relpath(p, ROOT) + " ")
                except Exception:  # noqa: BLE001
                    continue
        self.assertEqual(bad, [], "以下产物含客户端无法解析的 URL：\n" + "\n".join(bad[:20]))

    def test_keep_alive_lives_drops_empties(self):
        import merge as M
        lives = [
            {"name": "有引用", "url": "https://a.com/l"},
            {"name": "全非法", "channels": [{"name": "C", "urls": ["./x.txt"]}]},
            {"name": "部分", "channels": [{"name": "C1", "urls": ["./x.txt"]},
                                          {"name": "C2", "urls": ["https://a.com/1"]}]},
            {"name": "无内容"},
        ]
        out = M.keep_alive_lives(lives)
        names = [x["name"] for x in out]
        self.assertEqual(names, ["有引用", "部分"])
        self.assertEqual([c["name"] for c in out[1]["channels"]], ["C2"])


class TestRealArtifacts(unittest.TestCase):
    """针对真实产物与真实上游结果的断言。"""

    def test_public_tv_json_parses(self):
        p = os.path.join(ROOT, "public", "tv.json")
        if not os.path.exists(p):
            self.skipTest("尚未构建，先跑 python scripts/build.py")
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
        r = V.validate_tv_output(data)
        self.assertTrue(r["ok"], r["errors"])
        self.assertGreater(r["counts"]["sites"], 100, "站点数量异常偏少")

    def test_public_live_json_parses(self):
        p = os.path.join(ROOT, "public", "live.json")
        if not os.path.exists(p):
            self.skipTest("尚未构建")
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
        r = V.validate_live_output(data)
        self.assertTrue(r["ok"], r["errors"])
        self.assertGreater(r["counts"]["channels"], 100)

    def test_public_status_json(self):
        p = os.path.join(ROOT, "public", "status.json")
        if not os.path.exists(p):
            self.skipTest("尚未构建")
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
        self.assertTrue(V.validate_status(data)["ok"])
        self.assertEqual(data["summary"]["total_sources"], len(C.load_sources()["sources"]))

    def test_no_duplicate_keys_in_tv(self):
        p = os.path.join(ROOT, "public", "tv.json")
        if not os.path.exists(p):
            self.skipTest("尚未构建")
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
        keys = [s.get("key") for s in data["sites"] if s.get("key")]
        self.assertEqual(len(keys), len(set(keys)), "最终配置中存在重复 key")

    def test_at_least_two_tvbox_upstreams_succeeded(self):
        """实证：本次/最近一次运行，至少两个 TVBox 上游实际成功。"""
        st = C.load_state()
        if not st.get("sources"):
            self.skipTest("尚无抓取记录，先跑 python scripts/fetch.py")
        cfg = C.load_sources()
        tvbox_ids = {s["id"] for s in cfg["sources"]
                     if s["type"] == "tvbox" and s.get("enabled", True)}
        ok = [i for i in tvbox_ids
              if st["sources"].get(i, {}).get("status") in ("ok", "stale")]
        self.assertGreaterEqual(len(ok), 2,
                                f"TVBox 成功上游不足 2 个：实际 {ok}")
        for i in ok:
            self.assertTrue(os.path.exists(C.cache_path(i)), f"{i} 无缓存文件")

    def test_real_cache_payloads_validate(self):
        cfg = C.load_sources()
        n = 0
        for s in cfg["sources"]:
            p = C.cache_path(s["id"])
            if not os.path.exists(p):
                continue
            with open(p, "rb") as f:
                body = f.read()
            v = C.validate_payload(s, body)
            self.assertTrue(v["ok"], f"{s['id']} 缓存校验失败: {v.get('errors')}")
            n += 1
        if n == 0:
            self.skipTest("无缓存")


class TestKernelContract(unittest.TestCase):
    """锁住 TVBox 内核（FongMi）的字段类型契约。

    为什么这项测试至关重要 —— 源码实证：

        // Site.java
        public static Site objectFrom(JsonElement el, String spider) {
            try {
                Site site = App.gson().fromJson(el, Site.class);
                ...
            } catch (Exception e) {
                return new Site();          // ← 静默返回空对象，key = null
            }
        }

        // VodConfig.initSite
        Map<String, Site> items = Site.findAll().stream()
            .collect(Collectors.toMap(Site::getKey, Function.identity()));
            // ↑ HashMap.merge(null, v, fn) 抛 NullPointerException

    即：配置里只要有 **1 个** 站点的字段类型不符合 Site 的 gson 契约，
    就会产生 null key，`Collectors.toMap` 直接抛 NPE，
    **整份配置加载失败** —— 客户端提示「解析配置失败」。

    实测（2026-10-05，讴歌 6.0.9.3）：
        /tv            类型冲突 35  → 解析失败
        tv-standard    类型冲突 22  → 解析失败
        ysc_single_agg 类型冲突  0  → 正常加载
    """

    PROD = os.path.join(ROOT, "public")

    SITE_TYPES = {
        "key": str, "name": str, "api": str, "ext": str, "jar": str,
        "click": str, "playUrl": str,
        "type": int, "hide": int, "indexs": int, "timeout": int,
        "searchable": int, "changeable": int, "quickSearch": int, "danmaku": int,
        "categories": list, "header": dict, "style": dict, "selected": bool,
    }

    @staticmethod
    def _jt(v):
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            return "int"
        if isinstance(v, str):
            return "str"
        if isinstance(v, list):
            return "list"
        if isinstance(v, dict):
            return "dict"
        return type(v).__name__

    def _configs(self):
        """遍历 public/ 下所有「含 sites 的 JSON」配置。"""
        for dp, _, fs in os.walk(self.PROD):
            for fn in sorted(fs):
                if not fn.endswith(".json"):
                    continue
                p = os.path.join(dp, fn)
                try:
                    with open(p, encoding="utf-8-sig") as f:
                        d = json.load(f)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(d, dict) and isinstance(d.get("sites"), list):
                    yield os.path.relpath(p, ROOT), d

    def test_no_site_field_type_conflict(self):
        """任何站点字段类型不符 → 客户端整份配置加载失败。"""
        bad = []
        for name, d in self._configs():
            for s in d["sites"]:
                if not isinstance(s, dict):
                    bad.append(f"{name}: 站点不是对象")
                    continue
                for k, v in s.items():
                    if v is None:
                        continue
                    want = self.SITE_TYPES.get(k)
                    if want is None:
                        continue          # 未知字段 gson 会忽略
                    got = self._jt(v)
                    ok = ((want is str and got == "str")
                          or (want is int and got == "int")
                          or (want is list and got == "list")
                          or (want is dict and got == "dict")
                          or (want is bool and got in ("bool", "int")))
                    if not ok:
                        bad.append(f"{name}: {s.get('key')} 的 {k} "
                                   f"期望{want.__name__} 实际{got}")
        self.assertEqual(bad[:20], [],
                         "以下字段类型冲突会让客户端整份配置加载失败：" + chr(10)
                         + chr(10).join(bad[:20]))

    def test_keys_non_empty_and_unique(self):
        """空 key 会让 toMap 抛 NPE，重复 key 抛 IllegalStateException。"""
        for name, d in self._configs():
            keys = [str(s.get("key") or "") for s in d["sites"] if isinstance(s, dict)]
            self.assertNotIn("", keys, f"{name} 存在空 key 的站点")
            dups = {k for k in keys if keys.count(k) > 1}
            self.assertEqual(dups, set(), f"{name} 存在重复 key: {list(dups)[:5]}")

    def test_sanitizer_idempotent(self):
        """消毒器必须幂等：干净配置再过一遍不应有任何改动。"""
        import sanitize as S
        for name, d in self._configs():
            _fixed, st = S.sanitize_config(d)
            dirty = st["sites_fixed"] + st["sites_dropped"] + st["sites_dedup"]
            self.assertEqual(
                dirty, 0,
                f"{name} 未消毒干净：{st}（应在构建/部署前跑 scripts/sanitize.py）")

    def test_sanitizer_repairs_known_dirty_shapes(self):
        """回归：曾导致 /tv 解析失败的两类脏数据必须被修好。"""
        import sanitize as S
        dirty = {
            "sites": [
                # categories 是字符串（应为数组）—— 实测 22 处
                {"key": "a", "name": "A", "type": 0, "categories": "电影,电视剧"},
                # ext 是对象（应为字符串）—— 实测 13 处
                {"key": "b", "name": "B", "type": 3, "api": "http://x/y.js",
                 "ext": {"host": "http://h:1"}},
            ]
        }
        fixed, st = S.sanitize_config(dirty)
        self.assertEqual(st["sites_dropped"], 0)
        self.assertIsInstance(fixed["sites"][0]["categories"], list)
        self.assertEqual(fixed["sites"][0]["categories"], ["电影", "电视剧"])
        self.assertIsInstance(fixed["sites"][1]["ext"], str)
        self.assertTrue(fixed["sites"][1]["ext"].startswith("{"))


if __name__ == "__main__":
    unittest.main(verbosity=2)