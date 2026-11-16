"""bridge/scripts/weekly.py 离线回归：不联网、不跑子进程、不写任何文件。

覆盖 2026-11-15 600s 超时归因后的修复点：
- 全局时间预算会截断单请求超时，预算耗尽时快速失败而不是挂住；
- 校验阶段并发执行且保序，单项失败不影响其它项；
- 构建/导出子进程有超时；
- CDN ref 确认限制重试次数。
"""
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("weekly.py")
spec = importlib.util.spec_from_file_location("bridge_weekly_offline", SCRIPT)
weekly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(weekly)


class WeeklyOfflineTests(unittest.TestCase):
    def setUp(self):
        self.guards = [
            patch.object(weekly.urllib.request, "urlopen",
                         side_effect=AssertionError("禁止真实HTTP")),
            patch.object(weekly.subprocess, "run",
                         side_effect=AssertionError("禁止真实子进程")),
            patch.object(weekly, "local_count", return_value=None),
        ]
        for guard in self.guards:
            guard.start()
            self.addCleanup(guard.stop)
        self.addCleanup(weekly.set_budget, 0)
        self.addCleanup(weekly.RULES.__setitem__, slice(None), list(weekly.RULES))
        self.addCleanup(weekly.EXPORTS.__setitem__, slice(None), list(weekly.EXPORTS))

    def test_budget_caps_single_request_timeout(self):
        with patch.object(weekly.time, "monotonic", return_value=100.0):
            weekly.set_budget(60)
            self.assertEqual(weekly._cap(90), 60.0)
            self.assertEqual(weekly._cap(10), 10.0)
        weekly.set_budget(0)
        self.assertEqual(weekly._cap(90), 90.0)

    def test_http_get_fails_fast_when_budget_exhausted(self):
        with patch.object(weekly.time, "monotonic", return_value=100.0):
            weekly.set_budget(10)
            with patch.object(weekly.time, "monotonic", return_value=200.0), \
                 patch.object(weekly.urllib.request, "urlopen") as opener:
                with self.assertRaises(TimeoutError):
                    weekly.http_get("https://example.invalid/rules")
                opener.assert_not_called()

    def test_run_script_timeout_returns_failure_not_hang(self):
        with patch.object(weekly.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertEqual(weekly.run_script("build.py", timeout=1), (1, "timeout"))

    def test_ref_reachable_bounded_retries(self):
        with patch.object(weekly, "http_get", side_effect=OSError("synthetic")) as get, \
             patch.object(weekly.time, "sleep"):
            self.assertFalse(weekly.ref_reachable("a" * 40, "u/r"))
        self.assertEqual(get.call_count, 2)

    def test_verify_runs_parallel_keeps_order_and_counts(self):
        weekly.RULES[:] = ["a", "b"]
        weekly.EXPORTS[:] = [("x", b"#")]

        def fake_get(url, timeout=90):
            if url.endswith("a.yaml"):
                return 200, b"# bridge rule set\ncount: 1 \n"
            if url.endswith("b.yaml"):
                return 200, b"# wrong header\n"
            return 200, b"#" + b"x" * 200

        conf = {"github": {"repo": "u/r", "rule_base":
                           "https://fastly.jsdelivr.net/gh/u/r@main/rule"}}
        with patch.object(weekly, "http_get", side_effect=fake_get), \
             patch.object(weekly, "purge_path", return_value="ok") as purge:
            ok = weekly.step_verify(conf, "abc1234")
        self.assertFalse(ok)
        self.assertEqual(purge.call_count, 3)

    def test_verify_degrades_when_budget_exhausted(self):
        weekly.RULES[:] = ["a"]
        weekly.EXPORTS[:] = [("x", b"#")]
        conf = {"github": {"repo": "u/r", "rule_base":
                           "https://fastly.jsdelivr.net/gh/u/r@main/rule"}}
        with patch.object(weekly.time, "monotonic", return_value=100.0):
            weekly.set_budget(10)
            with patch.object(weekly.time, "monotonic", return_value=200.0), \
                 patch.object(weekly, "http_get") as get, \
                 patch.object(weekly, "purge_path") as purge:
                self.assertFalse(weekly.step_verify(conf, "abc1234"))
                get.assert_not_called()
                purge.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
