"""bridge/scripts/weekly.py 离线回归：不联网、不写任何文件。

仅硬超时用例运行本机模拟慢读取子进程，其余真实 HTTP / 子进程均被守卫阻断。

覆盖 2026-11-15 600s 超时归因后的修复点：
- 全局时间预算会截断单请求超时，预算耗尽时快速失败而不是挂住；
- 校验阶段并发执行且保序，单项失败不影响其它项；
- 构建/导出子进程有超时；
- CDN ref 确认限制重试次数。
"""
import contextlib
import gzip
import http.client
import io
import importlib.util
import threading
import time
import sys
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

REAL_SUBPROCESS_RUN = subprocess.run
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

    def test_cap_never_extends_expired_or_subsecond_budget(self):
        with patch.object(weekly.time, "monotonic", return_value=100.0):
            weekly.DEADLINE = 100.25
            self.assertEqual(weekly._cap(30), 0.25)
            weekly.DEADLINE = 100.0
            with self.assertRaises(TimeoutError):
                weekly._cap(30)

    def test_parallel_deadline_returns_named_timeout_and_preserves_success(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def work(item):
            if item == "slow":
                release.wait(2)
            return item
        start = time.monotonic()
        result = weekly._parallel_map(["fast", "slow"], work,
                                      deadline=start + 0.1)
        self.assertLess(time.monotonic() - start, 0.8)
        self.assertEqual(result[0], "fast")
        self.assertIsInstance(result[1], TimeoutError)
        release.set()

    def test_http_entire_read_is_bounded_by_child_timeout(self):
        with patch.object(weekly.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("http", 0.1)) as run:
            with self.assertRaisesRegex(TimeoutError, "http_total_timeout"):
                weekly.http_get("https://example.invalid/rules", timeout=0.1)
        self.assertEqual(run.call_args.kwargs["timeout"], 0.1)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_http_timeout_reports_last_confirmed_stage(self):
        for diagnostics, stage in ((b"", "open"), (b"open\n", "open"),
                                   (b"open\nread\n", "read"),
                                   (b"open\nread\nwrite\n", "write"),
                                   (b"open\nread\nwrite\nflush\n", "flush"),
                                   (b"open\nread\nwrite\nflush\ndone\n", "done")):
            with self.subTest(stage=stage, diagnostics=diagnostics), \
                 patch.object(weekly.subprocess, "run", side_effect=
                              subprocess.TimeoutExpired("http", 0.1, stderr=diagnostics)):
                with self.assertRaisesRegex(TimeoutError, "http_total_timeout:" + stage + "$"):
                    weekly.http_get("https://example.invalid/rules", timeout=0.1)

    def test_http_failure_reports_reason_not_stage_markers(self):
        result = subprocess.CompletedProcess([], 1, b"", b"open\nread\nIncompleteRead")
        with patch.object(weekly.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(OSError, "^http_request_failed: IncompleteRead$"):
                weekly.http_get("https://example.invalid/rules")

    def test_http_worker_result_and_limit_are_checked(self):
        result = subprocess.CompletedProcess([], 0, b"200\n# content", b"open\nread\n")
        with patch.object(weekly.subprocess, "run", return_value=result):
            self.assertEqual(weekly.http_get("https://example.invalid/rules"),
                             (200, b"# content"))
        result.returncode = 1
        result.stderr = b"http_body_too_large"
        with patch.object(weekly.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(OSError, "http_body_too_large"):
                weekly.http_get("https://example.invalid/rules")

    def test_real_slow_http_read_is_killed_within_total_timeout(self):
        slow_worker = '''
import time
import urllib.request
class SlowResponse:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, size):
        while True:
            time.sleep(0.05)
urllib.request.urlopen = lambda *args, **kwargs: SlowResponse()
'''
        start = time.monotonic()
        with patch.object(weekly, "HTTP_WORKER", slow_worker + weekly.HTTP_WORKER), \
             patch.object(weekly.subprocess, "run", side_effect=REAL_SUBPROCESS_RUN):
            with self.assertRaisesRegex(TimeoutError, "http_total_timeout:read"):
                weekly.http_get("https://example.invalid/rules", timeout=1)
        self.assertLess(time.monotonic() - start, 3)

    def test_http_worker_rejects_oversized_body_without_network(self):
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, limit):
                self.limit = limit
                return b"x" * limit
        response = Response()
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["worker", "https://example.invalid/rules", "1", "8"]), \
             patch.object(weekly.urllib.request, "urlopen", return_value=response), \
             contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                exec(compile(weekly.HTTP_WORKER, "http_worker", "exec"), {})
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(response.limit, 9)
        self.assertEqual(stderr.getvalue().splitlines()[-1], "http_body_too_large")

    def test_http_child_does_not_inherit_secret_environment(self):
        result = subprocess.CompletedProcess([], 0, b"200\n# content", b"")
        with patch.dict(weekly.os.environ, {"BRIDGE_HOSTS_JSON": "synthetic-secret"}), \
             patch.object(weekly.subprocess, "run", return_value=result) as run:
            weekly.http_get("https://example.invalid/rules")
        self.assertNotIn("BRIDGE_HOSTS_JSON", run.call_args.kwargs["env"])

    def test_build_and_git_leave_validation_reserve(self):
        result = subprocess.CompletedProcess([], 0, b"", b"")
        with patch.object(weekly.time, "monotonic", return_value=100.0):
            weekly.set_budget(weekly.VERIFY_RESERVE + 10)
            with patch.object(weekly.subprocess, "run", return_value=result) as run:
                weekly.run_script("build.py", timeout=240)
                self.assertEqual(run.call_args.kwargs["timeout"], 10)
                weekly.run_git(["status"], timeout=300)
                self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_verify_request_budget_tracks_size_without_unbounded_small_files(self):
        with patch.object(weekly.time, "monotonic", return_value=100.0):
            for size, expected in ((0, 30), (1024 * 1024 - 1, 30),
                                   (1024 * 1024, 60), (32 * 1024 * 1024, 60)):
                with self.subTest(size=size), \
                     patch.object(weekly.os.path, "getsize", return_value=size):
                    self.assertEqual(weekly._verify_request_timeout("local", 580), expected)
            with patch.object(weekly.os.path, "getsize", side_effect=FileNotFoundError):
                self.assertEqual(weekly._verify_request_timeout("missing", 580), 30)

    def test_large_verify_request_is_clipped_by_stage_and_global_budget(self):
        with patch.object(weekly.time, "monotonic", return_value=100.0), \
             patch.object(weekly.os.path, "getsize", return_value=3 * 1024 * 1024):
            weekly.set_budget(120)
            self.assertEqual(weekly._verify_request_timeout("large", 220), 60)
            self.assertEqual(weekly._verify_request_timeout("large", 112), 12)
            weekly.set_budget(7)
            self.assertEqual(weekly._verify_request_timeout("large", 220), 7)
            weekly.DEADLINE = 100.0
            with self.assertRaises(TimeoutError):
                weekly._verify_request_timeout("large", 220)

    def test_verify_stage_is_capped_at_120_and_passes_size_aware_budgets(self):
        weekly.RULES[:] = ["large", "small"]
        weekly.EXPORTS[:] = [("large.txt", b"#"), ("small.txt", b"#")]
        conf = {"github": {"repo": "u/r", "rule_base":
                           "https://fastly.jsdelivr.net/gh/u/r@main/rule"}}
        observed = []

        def collect(items, fn, **kwargs):
            self.assertEqual(kwargs["deadline"], min(weekly.DEADLINE, 220))
            return [fn(item) for item in items]

        def fake_get(url, timeout):
            observed.append(timeout)
            return 200, b"# bridge rule set\ncount: 1 \n" + b"x" * 200

        def size(path):
            return 3 * 1024 * 1024 if Path(path).stem == "large" else 100

        with patch.object(weekly.time, "monotonic", return_value=100.0), \
             patch.object(weekly.os.path, "getsize", side_effect=size), \
             patch.object(weekly, "_parallel_map", side_effect=collect), \
             patch.object(weekly, "http_get", side_effect=fake_get), \
             contextlib.redirect_stdout(io.StringIO()):
            weekly.set_budget(weekly.DEFAULT_BUDGET)
            self.assertEqual(weekly.VERIFY_RESERVE, 120)
            self.assertTrue(weekly.step_verify(conf, "abc1234", purge=False))
            self.assertEqual(observed, [60, 30, 60, 30])
            observed.clear()
            weekly.set_budget(15)
            self.assertTrue(weekly.step_verify(conf, "abc1234", purge=False))
            self.assertEqual(observed, [15, 15, 15, 15])

    def test_verify_uses_one_queue_for_all_22_items(self):
        conf = {"github": {"repo": "u/r", "rule_base":
                           "https://fastly.jsdelivr.net/gh/u/r@main/rule"}}
        def collect(items, fn, **kwargs):
            self.assertEqual(len(items), 22)
            self.assertIn("deadline", kwargs)
            return [TimeoutError("weekly_budget_exhausted") for _ in items]
        output = io.StringIO()
        with patch.object(weekly, "_parallel_map", side_effect=collect) as parallel, \
             contextlib.redirect_stdout(output):
            self.assertFalse(weekly.step_verify(conf, "abc1234", purge=False))
        self.assertEqual(parallel.call_count, 1)
        self.assertEqual(output.getvalue().count("失败 weekly_budget_exhausted"), 22)

    def _run_http_worker(self, wire, max_bytes=256):
        class MemorySocket:
            def makefile(self, mode):
                return io.BytesIO(wire)

        response = http.client.HTTPResponse(MemorySocket())
        response.begin()
        output = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        errors = io.StringIO()
        code = 0
        with patch.object(weekly.urllib.request, "urlopen", return_value=response), \
             patch.object(sys, "argv", ["worker", "https://example.invalid/rules", "1", str(max_bytes)]), \
             patch.object(sys, "stdout", output), \
             patch.object(sys, "stderr", errors):
            try:
                exec(compile(weekly.HTTP_WORKER, "<HTTP_WORKER>", "exec"), {})
            except SystemExit as exc:
                code = exc.code
        diagnostics = errors.getvalue().splitlines()
        self.assertEqual(diagnostics[:2], ["open", "read"])
        if code == 0:
            self.assertEqual(diagnostics[2:], ["write", "flush", "done"])
            return code, output.buffer.getvalue(), ""
        return code, output.buffer.getvalue(), "\n".join(diagnostics[2:])

    def test_http_worker_requests_gzip_without_changing_url(self):
        wire = b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc"
        with patch.object(weekly.urllib.request, "Request",
                          wraps=weekly.urllib.request.Request) as request:
            self.assertEqual(self._run_http_worker(wire), (0, b"200\nabc", ""))
        self.assertEqual(request.call_args.args, ("https://example.invalid/rules",))
        self.assertEqual(request.call_args.kwargs["headers"]["Accept-Encoding"], "gzip")

    def test_http_worker_accepts_identity_encoding(self):
        wire = (b"HTTP/1.1 200 OK\r\nContent-Encoding: identity\r\n"
                b"Content-Length: 3\r\n\r\nabc")
        self.assertEqual(self._run_http_worker(wire), (0, b"200\nabc", ""))

    def test_http_worker_decodes_complete_gzip_body(self):
        for body in (b"", b"# valid", b"x" * 256):
            with self.subTest(length=len(body)):
                compressed = gzip.compress(body)
                wire = (b"HTTP/1.1 200 OK\r\nContent-Encoding: GZip\r\nContent-Length: " +
                        str(len(compressed)).encode("ascii") + b"\r\n\r\n" + compressed)
                self.assertEqual(self._run_http_worker(wire), (0, b"200\n" + body, ""))

    def test_http_worker_rejects_truncated_gzip_even_with_matching_wire_length(self):
        compressed = gzip.compress(b"# valid")
        for body in (b"", compressed[:8], compressed[:-1], compressed[:-8]):
            with self.subTest(length=len(body)):
                wire = (b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: " +
                        str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
                code, output, error = self._run_http_worker(wire)
                self.assertEqual((code, output), (1, b""))
                self.assertIn(error, ("EOFError", "BadGzipFile"))

    def test_http_worker_rejects_gzip_crc_and_size_corruption(self):
        compressed = gzip.compress(b"# valid")
        for offset in (-8, -4):
            body = bytearray(compressed)
            body[offset] ^= 1
            wire = (b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n" + bytes(body))
            with self.subTest(offset=offset):
                self.assertEqual(self._run_http_worker(wire), (1, b"", "BadGzipFile"))

    def test_http_worker_gzip_read_is_bounded_and_rejects_expansion(self):
        max_bytes = weekly.MAX_HTTP_BYTES
        compressed = gzip.compress(b"x" * (max_bytes + 1))
        self.assertLess(len(compressed), max_bytes)
        wire = b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n" + compressed
        read = gzip.GzipFile.read
        sizes = []

        def bounded_read(stream, size=-1):
            sizes.append(size)
            self.assertEqual(size, max_bytes + 1)
            return read(stream, size)

        with patch.object(gzip.GzipFile, "read", bounded_read):
            self.assertEqual(self._run_http_worker(wire, max_bytes=max_bytes),
                             (1, b"", "http_body_too_large"))
        self.assertEqual(sizes, [max_bytes + 1])

    def test_http_worker_gzip_checks_all_members_and_rejects_trailing_junk(self):
        compressed = gzip.compress(b"first") + gzip.compress(b"second")
        prefix = b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n"
        self.assertEqual(self._run_http_worker(prefix + compressed),
                         (0, b"200\nfirstsecond", ""))
        expanded = gzip.compress(b"x" * 200) + gzip.compress(b"y" * 200)
        self.assertEqual(self._run_http_worker(prefix + expanded),
                         (1, b"", "http_body_too_large"))
        self.assertEqual(self._run_http_worker(prefix + compressed + b"junk"),
                         (1, b"", "BadGzipFile"))
        self.assertEqual(self._run_http_worker(prefix + compressed[:-1]),
                         (1, b"", "EOFError"))

    def test_http_worker_rejects_unknown_or_stacked_content_encoding(self):
        for encoding in (b"br", b"deflate", b"gzip, br", b"gzip, gzip"):
            with self.subTest(encoding=encoding):
                wire = b"HTTP/1.1 200 OK\r\nContent-Encoding: " + encoding + b"\r\n\r\nabc"
                self.assertEqual(self._run_http_worker(wire),
                                 (1, b"", "http_unsupported_encoding"))

    def test_http_worker_gzip_keeps_wire_length_and_chunked_checks(self):
        compressed = gzip.compress(b"# valid")
        prefix = b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n"
        length = str(len(compressed)).encode("ascii")
        self.assertEqual(self._run_http_worker(
            prefix + b"Content-Length: " + length + b"\r\n\r\n" + compressed[:-1]),
            (1, b"", "IncompleteRead"))
        self.assertEqual(self._run_http_worker(
            prefix + b"Content-Length: 257\r\n\r\n"),
            (1, b"", "http_body_too_large"))
        chunk = hex(len(compressed))[2:].encode("ascii") + b"\r\n" + compressed + b"\r\n"
        chunked = prefix + b"Transfer-Encoding: chunked\r\nContent-Length: 999\r\n\r\n"
        self.assertEqual(self._run_http_worker(chunked + chunk + b"0\r\n\r\n"),
                         (0, b"200\n# valid", ""))
        self.assertEqual(self._run_http_worker(chunked + chunk),
                         (1, b"", "IncompleteRead"))
        self.assertEqual(self._run_http_worker(chunked + chunk + b"0\r\n\r\n",
                                              max_bytes=len(compressed) - 1),
                         (1, b"", "http_body_too_large"))

    def test_http_worker_rejects_content_length_early_eof(self):
        for body in (b"# bridge rule set\ncount: 1 \n", b"#" + b"x" * 120):
            with self.subTest(body=body[:20]):
                wire = b"HTTP/1.1 200 OK\r\nContent-Length: 200\r\n\r\n" + body
                code, output, error = self._run_http_worker(wire)
                self.assertEqual(code, 1)
                self.assertEqual(output, b"")
                self.assertEqual(error, "IncompleteRead")

    def test_http_worker_accepts_complete_content_length(self):
        for body in (b"", b"# valid", b"x" * 256):
            with self.subTest(length=len(body)):
                wire = (b"HTTP/1.1 200 OK\r\nContent-Length: " +
                        str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
                self.assertEqual(self._run_http_worker(wire), (0, b"200\n" + body, ""))

    def test_http_worker_rejects_oversized_declared_length(self):
        wire = b"HTTP/1.1 200 OK\r\nContent-Length: 257\r\n\r\n"
        self.assertEqual(self._run_http_worker(wire), (1, b"", "http_body_too_large"))

    def test_http_worker_rejects_oversized_unknown_length(self):
        wire = b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 257
        self.assertEqual(self._run_http_worker(wire), (1, b"", "http_body_too_large"))

    def test_http_worker_rejects_incomplete_or_invalid_chunked(self):
        for chunks in (b"5\r\nabc", b"3\r\nabc\r\n", b"invalid\r\n"):
            with self.subTest(chunks=chunks):
                wire = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunks
                self.assertEqual(self._run_http_worker(wire), (1, b"", "IncompleteRead"))

    def test_http_worker_accepts_complete_chunked_and_enforces_limit(self):
        wire = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                b"Content-Length: 999\r\n\r\n3\r\nabc\r\n0\r\n\r\n")
        self.assertEqual(self._run_http_worker(wire), (0, b"200\nabc", ""))
        self.assertEqual(self._run_http_worker(wire, max_bytes=2),
                         (1, b"", "http_body_too_large"))

    def test_verify_all_22_failures_remain_visible(self):
        conf = {"github": {"repo": "u/r", "rule_base":
                           "https://fastly.jsdelivr.net/gh/u/r@main/rule"}}
        output = io.StringIO()
        with patch.object(weekly, "http_get", side_effect=OSError("synthetic")) as get, \
             contextlib.redirect_stdout(output):
            self.assertFalse(weekly.step_verify(conf, "abc1234", purge=False))
        self.assertEqual(get.call_count, 22)
        self.assertIn("成功 0 / 失败 22", output.getvalue())
        for name in weekly.RULES + [name for name, _ in weekly.EXPORTS]:
            self.assertIn(name, output.getvalue())

    def test_purge_is_skipped_to_reserve_validation_time(self):
        conf = {"github": {"repo": "u/r", "rule_base":
                           "https://fastly.jsdelivr.net/gh/u/r@main/rule"}}
        weekly.set_budget(weekly.VERIFY_RESERVE)
        with patch.object(weekly, "purge_path") as purge, \
             patch.object(weekly, "http_get", side_effect=OSError("synthetic")) as get:
            self.assertFalse(weekly.step_verify(conf, "abc1234"))
        purge.assert_not_called()
        self.assertEqual(get.call_count, 22)

    def test_verify_only_does_not_load_secrets_build_or_push_and_returns_failure(self):
        with patch.dict(weekly.os.environ, dict(weekly.os.environ), clear=True), \
             patch.object(sys, "argv", [str(SCRIPT), "--verify-only", "--skip-purge"]), \
             patch.object(weekly, "load_conf", side_effect=AssertionError("禁止读凭据")), \
             patch.object(weekly, "step_build", side_effect=AssertionError("禁止构建")), \
             patch.object(weekly, "step_push", side_effect=AssertionError("禁止推送")), \
             patch.object(weekly, "step_verify", return_value=False) as verify:
            self.assertEqual(weekly.main(), 1)
        self.assertFalse(verify.call_args.kwargs["purge"])

    def test_main_returns_nonzero_when_verify_fails(self):
        conf = {"github": {"repo": "u/r"}}
        with patch.object(sys, "argv", [str(SCRIPT), "--no-push"]), \
             patch.object(weekly, "load_conf", return_value=conf), \
             patch.object(weekly, "step_fetch"), \
             patch.object(weekly, "step_build"), \
             patch.object(weekly, "step_export", return_value=True), \
             patch.object(weekly, "step_verify", return_value=False):
            self.assertEqual(weekly.main(), 1)

    def test_verify_only_matches_production_direct_route(self):
        def verify(*args, **kwargs):
            self.assertEqual(weekly.os.environ.get('NO_PROXY'), '*')
            self.assertFalse(any(key.lower() in ('http_proxy', 'https_proxy', 'all_proxy')
                                 for key in weekly.os.environ))
            return True

        with patch.dict(weekly.os.environ, {
                'http_proxy': 'http://example.invalid:1',
                'HTTPS_PROXY': 'http://example.invalid:2',
                'All_Proxy': 'http://example.invalid:3', 'no_proxy': 'example.invalid'}), \
             patch.object(sys, 'argv', ['weekly.py', '--verify-only', '--skip-purge']), \
             patch.object(weekly, 'step_verify', side_effect=verify), \
             patch.object(weekly, 'load_conf', side_effect=AssertionError('no secrets')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(weekly.main(), 0)

    def test_retry_only_transient_failures_once(self):
        weekly.RULES[:] = ['a']
        weekly.EXPORTS[:] = []
        conf = {'github': {'repo': 'u/r', 'rule_base': 'https://example.invalid/rule'}}
        valid = (200, b'# bridge rule set\ncount: 1 \n')
        cases = [([TimeoutError('http_total_timeout:read'), valid], True, 2),
                 ([TimeoutError('http_total_timeout:open')] * 2, False, 2),
                 ([OSError('http_request_failed: IncompleteRead')], False, 1),
                 ([(200, b'bad content')], False, 1),
                 ([valid], True, 1)]
        for replies, expected, calls in cases:
            with self.subTest(replies=replies), \
                 patch.object(weekly, 'http_get', side_effect=replies) as get, \
                 contextlib.redirect_stdout(io.StringIO()):
                weekly.set_budget(120)
                self.assertEqual(weekly.step_verify(conf, 'abc1234', purge=False), expected)
                self.assertEqual(get.call_count, calls)

    def test_retry_skipped_when_stage_budget_insufficient(self):
        weekly.RULES[:] = ['a']
        weekly.EXPORTS[:] = []
        conf = {'github': {'repo': 'u/r', 'rule_base': 'https://example.invalid/rule'}}
        with patch.object(weekly, 'http_get', side_effect=TimeoutError('http_total_timeout:read')) as get, \
             contextlib.redirect_stdout(io.StringIO()):
            weekly.set_budget(4)
            self.assertFalse(weekly.step_verify(conf, 'abc1234', purge=False))
            self.assertEqual(get.call_count, 1)

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
