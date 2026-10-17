import json
import sys
import tempfile
import unittest
from pathlib import Path


KIT = Path(__file__).resolve().parents[1]


class VerificationCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / ".orchestra").mkdir()
        (self.project / ".orchestra/project.toml").write_text(
            (KIT / "templates/project.toml").read_text(), encoding="utf-8"
        )
        (self.project / "app.py").write_text("answer = 42\n", encoding="utf-8")

    def context(self) -> dict:
        return {
            "job": {"goal": "return the answer"},
            "acceptance": ["returns 42"],
            "checks": [{"argv": [sys.executable, "-c", "print('ok')"], "failure_cause": "implementation"}],
            "environment": {"python": "fixture"},
            "dependencies": {"stdlib": True},
            "settings": {"profile": "cheap"},
        }

    def evidence(self, *, review: bool = False) -> str:
        from orchestra_kit.evidence import create_evidence, run_evidence

        checked = run_evidence(
            self.project,
            ["app.py", ".orchestra/project.toml"],
            [{"argv": [sys.executable, "-c", "print('ok')"], "failure_cause": "implementation"}],
        )
        self.assertEqual(checked["status"], "verified")
        if not review:
            return checked["evidence_ref"]
        report = self.project / "review.md"
        report.write_text("VERDICT: PASS\nFixture independent review.\n", encoding="utf-8")
        return create_evidence(
            self.project,
            ["app.py", ".orchestra/project.toml"],
            checked["checks"],
            review_verdict="PASS",
            review_report=report,
        )

    def test_exact_context_and_subjects_reuse_passing_evidence(self) -> None:
        from orchestra_kit.verification_cache import find_verification, store_verification

        evidence_ref = self.evidence()
        stored = store_verification(
            self.project, files=["app.py"], context=self.context(),
            evidence_ref=evidence_ref, kind="check",
        )
        hit = find_verification(
            self.project, files=["app.py"], context=self.context(), kind="check",
        )
        self.assertTrue(stored["stored"])
        self.assertTrue(hit["reused"])
        self.assertEqual(hit["evidence_ref"], evidence_ref)
        self.assertEqual(hit["cache_key"], stored["cache_key"])
        self.assertEqual((self.project / stored["cache_ref"]).stat().st_mode & 0o077, 0)

    def test_cache_never_persists_context_text(self) -> None:
        from orchestra_kit.verification_cache import store_verification

        secret = "verification-cache-secret-marker-4e928"
        context = self.context()
        context["job"]["token"] = secret
        context["environment"]["credential"] = secret
        store_verification(
            self.project, files=["app.py"], context=context,
            evidence_ref=self.evidence(), kind="check",
        )
        cache = self.project / ".orchestra/verification-cache"
        self.assertTrue(any(cache.rglob("*.json")))
        for path in cache.rglob("*.json"):
            self.assertNotIn(secret, path.read_text(encoding="utf-8"))

    def test_any_declared_execution_input_change_misses(self) -> None:
        from orchestra_kit.verification_cache import find_verification, store_verification

        store_verification(
            self.project, files=["app.py"], context=self.context(),
            evidence_ref=self.evidence(), kind="check",
        )
        for key, value in (
            ("job", {"goal": "other"}),
            ("acceptance", ["returns 43"]),
            ("checks", [{"argv": [sys.executable, "-c", "raise SystemExit(1)"], "failure_cause": "implementation"}]),
            ("environment", {"python": "other"}),
            ("dependencies", {"stdlib": False}),
            ("settings", {"profile": "strong"}),
        ):
            changed = self.context()
            changed[key] = value
            with self.subTest(key=key):
                self.assertIsNone(find_verification(
                    self.project, files=["app.py"], context=changed, kind="check",
                ))
        (self.project / "app.py").write_text("answer = 43\n", encoding="utf-8")
        self.assertIsNone(find_verification(
            self.project, files=["app.py"], context=self.context(), kind="check",
        ))

    def test_configuration_and_policy_are_automatic_subjects(self) -> None:
        from orchestra_kit.evidence import run_evidence
        from orchestra_kit.verification_cache import find_verification, store_verification

        config = self.project / ".orchestra/project.toml"
        config.write_text(
            config.read_text(encoding="utf-8").replace("policy_files = []", "policy_files = ['policy.md']"),
            encoding="utf-8",
        )
        (self.project / "policy.md").write_text("# Policy\n", encoding="utf-8")
        checked = run_evidence(
            self.project,
            ["app.py", ".orchestra/project.toml", "policy.md"],
            [{"argv": [sys.executable, "-c", "pass"], "failure_cause": "implementation"}],
        )
        context = self.context()
        context["checks"] = [{"argv": [sys.executable, "-c", "pass"], "failure_cause": "implementation"}]
        with self.assertRaisesRegex(ValueError, "cover"):
            store_verification(
                self.project, files=["app.py"], context=context,
                evidence_ref=self.evidence(), kind="check",
            )
        store_verification(
            self.project, files=["app.py"], context=context,
            evidence_ref=checked["evidence_ref"], kind="check",
        )
        (self.project / "policy.md").write_text("# Changed policy\n", encoding="utf-8")
        self.assertIsNone(find_verification(
            self.project, files=["app.py"], context=context, kind="check",
        ))

    def test_changed_artifact_and_review_report_invalidate_hits(self) -> None:
        from orchestra_kit.verification_cache import find_verification, store_verification

        check_ref = self.evidence()
        stored = store_verification(
            self.project, files=["app.py"], context=self.context(),
            evidence_ref=check_ref, kind="check",
        )
        certificate = json.loads((self.project / check_ref).read_text(encoding="utf-8"))
        Path(certificate["checks"][0]["log"]).write_text("changed", encoding="utf-8")
        self.assertIsNone(find_verification(
            self.project, files=["app.py"], context=self.context(), kind="check",
        ))
        self.assertTrue(stored["stored"])

        review_ref = self.evidence(review=True)
        store_verification(
            self.project, files=["app.py"], context=self.context(),
            evidence_ref=review_ref, kind="review",
        )
        (self.project / "review.md").write_text("VERDICT: PASS\nChanged.\n", encoding="utf-8")
        self.assertIsNone(find_verification(
            self.project, files=["app.py"], context=self.context(), kind="review",
        ))

    def test_kind_separation_and_untrusted_cache_entries_fail_closed(self) -> None:
        from orchestra_kit.verification_cache import find_verification, store_verification

        check_ref = self.evidence()
        with self.assertRaisesRegex(ValueError, "review"):
            store_verification(
                self.project, files=["app.py"], context=self.context(),
                evidence_ref=check_ref, kind="review",
            )
        stored = store_verification(
            self.project, files=["app.py"], context=self.context(),
            evidence_ref=check_ref, kind="check",
        )
        cache = self.project / stored["cache_ref"]
        cache.write_text("not json", encoding="utf-8")
        self.assertIsNone(find_verification(
            self.project, files=["app.py"], context=self.context(), kind="check",
        ))
        cache.unlink()
        outside = self.project / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        cache.symlink_to(outside)
        self.assertIsNone(find_verification(
            self.project, files=["app.py"], context=self.context(), kind="check",
        ))
        cache.unlink()
        cache.parent.rmdir()
        cache_root = cache.parent.parent
        cache_root.rmdir()
        outside_cache = self.project / "outside-cache"
        (outside_cache / "check").mkdir(parents=True)
        cache_root.symlink_to(outside_cache, target_is_directory=True)
        self.assertIsNone(find_verification(
            self.project, files=["app.py"], context=self.context(), kind="check",
        ))

    def test_unknown_context_cannot_authorize_reuse(self) -> None:
        from orchestra_kit.verification_cache import find_verification, store_verification

        incomplete = {"job": {"goal": "missing inputs"}}
        self.assertIsNone(find_verification(
            self.project, files=["app.py"], context=incomplete, kind="check",
        ))
        with self.assertRaisesRegex(ValueError, "context"):
            store_verification(
                self.project, files=["app.py"], context=incomplete,
                evidence_ref=self.evidence(), kind="check",
            )

    def test_context_must_be_populated_and_checks_must_match_certificate(self) -> None:
        from orchestra_kit.verification_cache import find_verification, store_verification

        for key, value in (("job", None), ("job", {}), ("acceptance", None),
                           ("acceptance", []), ("checks", None), ("checks", []),
                           ("environment", None), ("environment", {}), ("settings", None),
                           ("settings", {}), ("dependencies", None)):
            context = self.context()
            context[key] = value
            with self.subTest(key=key):
                self.assertIsNone(find_verification(
                    self.project, files=["app.py"], context=context, kind="check",
                ))
                with self.assertRaisesRegex(ValueError, "context"):
                    store_verification(
                        self.project, files=["app.py"], context=context,
                        evidence_ref=self.evidence(), kind="check",
                    )

        for explicit_dependencies in ([], {}):
            context = self.context()
            context["dependencies"] = explicit_dependencies
            stored = store_verification(
                self.project, files=["app.py"], context=context,
                evidence_ref=self.evidence(), kind="check",
            )
            self.assertTrue(stored["stored"])

        mismatch = self.context()
        mismatch["checks"] = [{"argv": [sys.executable, "-c", "pass"], "failure_cause": "implementation"}]
        with self.assertRaisesRegex(ValueError, "checks"):
            store_verification(
                self.project, files=["app.py"], context=mismatch,
                evidence_ref=self.evidence(), kind="check",
            )
