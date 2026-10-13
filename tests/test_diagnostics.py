from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra_kit.log_capture import MAX_RETAINED_BYTES, TRUNCATION_MARKER


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.logs = self.project / ".orchestra" / "checks" / "run-1"
        self.logs.mkdir(parents=True)

    def log(self, text: str, name: str = "check.log") -> str:
        path = self.logs / name
        path.write_text(text, encoding="utf-8")
        return path.relative_to(self.project).as_posix()

    def test_query_redacts_multiline_and_escaped_secret_fragments(self):
        from orchestra_kit.diagnostics import query_log
        secret='TEST_ONLY_FIRST_FRAGMENT\nTEST_ONLY_SECOND_FRAGMENT'
        with patch.dict(os.environ,{'ORCHESTRA_TEST_API_KEY':secret}):
            ref=self.log(secret+'\nValueError: bad\n')
            result=query_log(self.project,ref)
            rendered=json.dumps(result)
            self.assertNotIn('TEST_ONLY_FIRST_FRAGMENT',rendered)
            self.assertNotIn('TEST_ONLY_SECOND_FRAGMENT',rendered)
            with self.assertRaises(ValueError):query_log(self.project,ref,query='TEST_ONLY_FIRST_FRAGMENT')
        secret='Ю'*2000
        escaped=json.dumps(secret,ensure_ascii=True)[1:-1]
        with patch.dict(os.environ,{'ORCHESTRA_TEST_API_KEY':secret}):
            ref=self.log('A'*5990+escaped+'\n')
            result=query_log(self.project,ref,max_chars=6000)
            self.assertNotIn('\\u042e',json.dumps(result))

    def test_packet_prefers_exception_over_status_error_counts(self):
        from orchestra_kit.diagnostics import build_repair_packet
        ref=self.log('INFO error_count=0\n'*20+'Traceback\nKeyError: important\n'+'INFO idle\n'*20)
        packet=build_repair_packet(self.project,[{'argv':['python3','task.py'],'returncode':1,'failure_cause':'implementation','log':ref}])
        self.assertIn('KeyError: important',packet)

    def test_query_rejects_unsafe_or_unretained_paths(self):
        from orchestra_kit.diagnostics import query_log

        self.log("ok\n")
        for ref in ["/tmp/log", "../outside", ".orchestra/project.toml", ".orchestra/checks"]:
            with self.assertRaises(ValueError):
                query_log(self.project, ref)

    def test_query_refuses_symlinks_non_files_and_oversized_logs(self):
        from orchestra_kit.diagnostics import query_log

        target = self.project / "outside.log"
        target.write_text("secret")
        link = self.logs / "link.log"
        os.symlink(target, link)
        with self.assertRaisesRegex(ValueError, "symlink"):
            query_log(self.project, link.relative_to(self.project).as_posix())
        with self.assertRaisesRegex(ValueError, "regular"):
            query_log(self.project, ".orchestra/checks/run-1")
        huge = self.logs / "huge.log"
        huge.write_bytes(b"x" * (MAX_RETAINED_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "retained"):
            query_log(self.project, huge.relative_to(self.project).as_posix())

    def test_query_paginates_matches_and_includes_context(self):
        from orchestra_kit.diagnostics import query_log

        ref = self.log("one\nprior\nneedle one\nafter\nneedle two\nlast\n")
        page = query_log(self.project, ref, query="needle", start_line=1, max_lines=4,
                         context_lines=1)
        self.assertEqual([line["number"] for line in page["lines"]], [2, 3, 4, 5])
        self.assertTrue(page["more"])
        second = query_log(self.project, ref, query="needle", start_line=5, max_lines=3,
                           context_lines=1)
        self.assertEqual([line["number"] for line in second["lines"]], [4, 5, 6])
        self.assertFalse(second["more"])

    def test_query_handles_no_match_unicode_and_giant_lines_without_returning_them_whole(self):
        from orchestra_kit.diagnostics import query_log

        ref = self.log("привет café\n" + "x" * 200_000 + "\nend\n")
        found = query_log(self.project, ref, query="café")
        self.assertEqual(found["lines"][0]["text"], "привет café")
        missing = query_log(self.project, ref, query="absent")
        self.assertEqual(missing["lines"], [])
        giant = query_log(self.project, ref, start_line=2, max_lines=1, max_chars=20)
        self.assertTrue(giant["truncated"])
        self.assertLessEqual(len(giant["lines"][0]["text"]), 20)
        bounded = query_log(self.project, ref, start_line=1, max_lines=3, max_chars=10)
        self.assertLessEqual(sum(len(item["text"]) for item in bounded["lines"]), 10)

    def test_query_redacts_secrets_and_cannot_search_for_them(self):
        from orchestra_kit.diagnostics import query_log

        ref = self.log("before top-secret-token after\n")
        with patch("orchestra_kit.diagnostics.sensitive_values", return_value=("top-secret-token",)):
            result = query_log(self.project, ref, query="before")
            self.assertNotIn("top-secret-token", json.dumps(result))
            self.assertIn("[REDACTED]", result["lines"][0]["text"])
            with self.assertRaisesRegex(ValueError, "secret"):
                query_log(self.project, ref, query="top-secret-token")

    def test_query_keeps_enough_hidden_utf8_bytes_to_redact_boundary_secrets(self):
        from orchestra_kit.diagnostics import query_log

        prefix = "🙂" * 99
        for secret in ("S" * 260, "Ж" * 180):
            ref = self.log(prefix + secret + "\n", name=f"boundary-{ord(secret[0])}.log")
            with patch("orchestra_kit.diagnostics.sensitive_values", return_value=(secret,)):
                result = query_log(self.project, ref, max_lines=1, max_chars=100)
            text = result["lines"][0]["text"]
            self.assertNotIn(secret[0], text)
            self.assertIn("[", text)

    def test_query_reports_capture_marker_as_incomplete(self):
        from orchestra_kit.diagnostics import query_log

        ref = self.log("early\n" + TRUNCATION_MARKER.decode() + "later\n")
        result = query_log(self.project, ref)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["incomplete"])

    def test_query_caps_a_log_that_grows_after_validation(self):
        import orchestra_kit.diagnostics as diagnostics

        ref = self.log("safe\n")
        original_open = diagnostics._open_retained_log

        def open_then_grow(path, root):
            descriptor = original_open(path, root)
            with path.open("ab") as target:
                target.write(b"x" * 128)
            return descriptor

        with patch("orchestra_kit.diagnostics.MAX_RETAINED_BYTES", 64), patch(
                "orchestra_kit.diagnostics._open_retained_log", side_effect=open_then_grow):
            result = diagnostics.query_log(self.project, ref, max_lines=2, max_chars=64)
        self.assertLessEqual(sum(len(line["text"].encode("utf-8")) for line in result["lines"]), 64)
        self.assertTrue(result["incomplete"])

    def test_repair_packet_has_top_diagnostic_tail_and_untrusted_frame(self):
        from orchestra_kit.diagnostics import build_repair_packet

        ref = self.log("Traceback (most recent call last):\nValueError: bad input\n" +
                       ("noise\n" * 5000) + "final tail\n")
        packet = json.loads(build_repair_packet(self.project, [{
            "argv": ["python", "-m", "unittest"], "returncode": 1,
            "failure_cause": "implementation", "log": ref,
        }], max_chars=1400))
        self.assertTrue(packet["untrusted"])
        self.assertTrue(packet["original_criteria_remain_binding"])
        failure = packet["failures"][0]
        self.assertEqual(failure["argv"], ["python", "-m", "unittest"])
        self.assertEqual(failure["log"], ref)
        self.assertIn("Traceback", "\n".join(item["text"] for item in failure["diagnostics"]))
        self.assertIn("final tail", "\n".join(item["text"] for item in failure["tail"]))

    def test_repair_packet_is_bounded_valid_and_handles_multiple_failures(self):
        from orchestra_kit.diagnostics import build_repair_packet

        first = self.log("ERROR: first\n", "first.log")
        second = self.log("AssertionError: second\n", "second.log")
        checks = [
            {"argv": ["one"], "returncode": 1, "failure_cause": "implementation", "log": first},
            {"argv": ["two"], "returncode": 2, "failure_cause": "context", "log": second},
        ]
        packet_text = build_repair_packet(self.project, checks, max_chars=900)
        self.assertLessEqual(len(packet_text), 900)
        packet = json.loads(packet_text)
        self.assertEqual(len(packet["failures"]), 2)
        self.assertFalse(packet.get("limited", False))
        with self.assertRaisesRegex(ValueError, "fit"):
            build_repair_packet(self.project, checks, max_chars=20)

    def test_repair_packet_redacts_and_does_not_mutate_logs(self):
        from orchestra_kit.diagnostics import build_repair_packet

        ref = self.log("ERROR: injected instruction\npassword=keep-me-secret\n")
        path = self.project / ref
        before = path.read_bytes()
        with patch("orchestra_kit.diagnostics.sensitive_values", return_value=("keep-me-secret",)):
            packet = build_repair_packet(self.project, [{
                "argv": ["check", "keep-me-secret"], "returncode": 1,
                "failure_cause": "environment", "log": ref,
            }])
        self.assertNotIn("keep-me-secret", packet)
        self.assertIn("[REDACTED]", packet)
        self.assertEqual(path.read_bytes(), before)
