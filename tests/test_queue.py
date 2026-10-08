from __future__ import annotations

import importlib
import importlib.util
import multiprocessing
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


KIT = Path(__file__).resolve().parents[1]


def _claim_process(project_name: str, output: multiprocessing.Queue) -> None:
    queue = importlib.import_module("orchestra_kit.queue").Queue(Path(project_name), KIT)
    claim = queue.claim("race", "worker" + str(os.getpid()))
    output.put(claim is not None)


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        (self.project / ".orchestra").mkdir(parents=True)
        self.config = self.project / ".orchestra" / "project.toml"
        self.config.write_text((KIT / "templates" / "project.toml").read_text(), encoding="utf-8")
        self.assertIsNotNone(importlib.util.find_spec("orchestra_kit.queue"))
        self.module = importlib.import_module("orchestra_kit.queue")
        self.queue = self.module.Queue(self.project, KIT)

    @staticmethod
    def node(node_id: str, *, depends_on=None, estimated_tokens=None, **extra: object) -> dict:
        value = {"id": node_id, "depends_on": depends_on or [], "job": {"role": "worker"},
                 "acceptance_refs": ["docs/acceptance.md"], "estimated_tokens": estimated_tokens}
        value.update(extra)
        return value

    def submit(self, nodes: list[dict], graph_id="graph") -> None:
        self.queue.submit(graph_id, nodes)

    def test_claim_refuses_policy_changed_during_config_load(self):
        self.config.write_text(self.config.read_text().replace('max_parallel = 3','max_parallel = 2'))
        self.submit([self.node('a'),self.node('b')])
        self.assertIsNotNone(self.queue.claim('graph','first'))
        original=self.queue._config
        def changed():
            loaded=original()
            self.config.write_text(self.config.read_text().replace('max_parallel = 2','max_parallel = 1'))
            return loaded
        with patch.object(self.queue,'_config',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'configuration changed'):
                self.queue.claim('graph','second')
        self.assertEqual(sum(n['status']=='running' for n in self.queue.inspect('graph')['tasks']),1)

    def test_submit_validates_immutable_dag_and_rejects_unsafe_inputs(self) -> None:
        self.submit([self.node("root"), self.node("child", depends_on=["root"])])
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.submit([self.node("other")])
        for nodes in (
            [self.node("a", depends_on=["missing"])],
            [self.node("a", depends_on=["a"])],
            [self.node("a", depends_on=["b"]), self.node("b", depends_on=["a"])],
            [self.node("a"), self.node("a")],
            [self.node("a", acceptance_refs=[])],
            [self.node("a", estimated_tokens=True)],
        ):
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                self.queue.submit("bad" + str(len(str(nodes))), nodes)

    def test_dependency_capacity_and_fenced_completion(self) -> None:
        # A valid DAG need not be supplied in topological order.
        self.submit([self.node("child", depends_on=["root"]), self.node("root")])
        claim = self.queue.claim("graph", "alice")
        self.assertEqual(claim["id"], "root")
        self.assertIsNone(self.queue.claim("graph", "bob"))
        with self.assertRaisesRegex(ValueError, "lease"):
            self.queue.finish("graph", "root", "alice", "wrong", "verified", "evidence", 2)
        self.queue.finish("graph", "root", "alice", claim["lease_token"], "verified", "evidence", 2)
        child = self.queue.claim("graph", "bob")
        self.assertEqual(child["id"], "child")
        self.assertEqual(self.queue.inspect("graph")["tasks"][0]["acceptance_refs"], ["docs/acceptance.md"])

    def test_failed_dependency_blocks_descendant_and_cancellation_drains_running(self) -> None:
        self.submit([self.node("root"), self.node("child", depends_on=["root"])])
        root = self.queue.claim("graph", "alice")
        self.queue.finish("graph", "root", "alice", root["lease_token"], "failed", None, 1)
        state = {task["id"]: task for task in self.queue.inspect("graph")["tasks"]}
        self.assertEqual(state["child"]["status"], "blocked")
        self.submit([self.node("work")], "cancel")
        work = self.queue.claim("cancel", "alice")
        self.queue.cancel("cancel", "work")
        self.assertEqual(self.queue.inspect("cancel")["tasks"][0]["status"], "running")
        self.queue.finish("cancel", "work", "alice", work["lease_token"], "verified", "evidence", 1)
        self.assertEqual(self.queue.inspect("cancel")["tasks"][0]["status"], "cancelled")

    def test_expiry_never_replays_or_releases_capacity_without_explicit_recovery(self) -> None:
        self.config.write_text(self.config.read_text().replace("max_parallel = 3", "max_parallel = 1"), encoding="utf-8")
        self.submit([self.node("one")], "one")
        self.submit([self.node("two")], "two")
        first = self.queue.claim("one", "alice", lease_seconds=1)
        self.assertIsNotNone(first)
        with self.assertRaisesRegex(ValueError, "lease"):
            self.queue.heartbeat("one", "one", "alice", first["lease_token"], lease_seconds=0)
        self.assertIsNone(self.queue.claim("two", "bob"))
        with self.assertRaisesRegex(ValueError, "authorized"):
            self.queue.recover("one", "one", execution_absent=True, descendants_absent=True, authorized=False, disposition="retry")
        with patch.object(self.module.time, "time", return_value=first["lease_expires"] + 1):
            self.queue.recover("one", "one", execution_absent=True, descendants_absent=True, authorized=True, disposition="retry")
        self.assertIsNotNone(self.queue.claim("two", "bob"))

    def test_two_processes_cannot_exceed_global_capacity(self) -> None:
        self.config.write_text(self.config.read_text().replace("max_parallel = 3", "max_parallel = 1"), encoding="utf-8")
        self.submit([self.node("one"), self.node("two")], "race")
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        processes = [context.Process(target=_claim_process, args=(str(self.project), output)) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(output.get(timeout=2) for _ in processes), 1)

    def test_recovered_attempt_remains_unknown_after_later_retry_completion(self) -> None:
        self.submit([self.node("work", estimated_tokens=1)])
        first = self.queue.claim("graph", "alice", lease_seconds=1)
        with patch.object(self.module.time, "time", return_value=first["lease_expires"] + 1):
            self.queue.recover("graph", "work", execution_absent=True, descendants_absent=True, authorized=True, disposition="retry")
        retry = self.queue.claim("graph", "bob")
        self.queue.finish("graph", "work", "bob", retry["lease_token"], "verified", "evidence", 7)
        self.config.write_text(self.config.read_text().replace("# max_observed_tokens = 200000", "max_observed_tokens = 20"), encoding="utf-8")
        self.assertIsNone(self.queue.claim("graph", "bob"))
        self.assertEqual(self.queue.inspect("graph")["budget"]["coverage"], "unknown")

    def test_bounds_and_missing_graph_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown graph"):
            self.queue.claim("missing", "alice")
        with self.assertRaisesRegex(ValueError, "12000"):
            self.submit([self.node("large", job={"text": "x" * 12001})])
        with self.assertRaisesRegex(ValueError, "6000"):
            self.submit([self.node("refs", acceptance_refs=["x" * 6001])])
        with self.assertRaisesRegex(ValueError, "256"):
            self.submit([self.node("n" + str(index)) for index in range(257)])

    def test_cancel_requested_expired_work_cannot_be_retried(self) -> None:
        self.submit([self.node("work")])
        claim = self.queue.claim("graph", "alice", lease_seconds=1)
        self.queue.cancel("graph", "work")
        with patch.object(self.module.time, "time", return_value=claim["lease_expires"] + 1):
            with self.assertRaisesRegex(ValueError, "cannot be retried"):
                self.queue.recover("graph", "work", execution_absent=True, descendants_absent=True, authorized=True, disposition="retry")

    def test_budget_launches_unknown_usage_and_source_or_input_block_prevent_claim(self) -> None:
        self.config.write_text(self.config.read_text().replace("# max_observed_tokens = 200000", "max_observed_tokens = 5\nmax_launches = 1"), encoding="utf-8")
        self.submit([self.node("a", estimated_tokens=5)], "a")
        first = self.queue.claim("a", "alice")
        self.assertIsNotNone(first)
        self.queue.finish("a", "a", "alice", first["lease_token"], "failed", None, None)
        self.submit([self.node("b")], "b")
        self.assertIsNone(self.queue.claim("b", "bob"))
        inspected = self.queue.inspect("b")
        self.assertEqual(inspected["budget"]["coverage"], "unknown")
        self.submit([self.node("blocked", input_blocked=True)], "blocked")
        self.assertIsNone(self.queue.claim("blocked", "alice"))
        self.assertEqual(self.queue.inspect("blocked")["tasks"][0]["block_reason"], "input_blocked")

    def test_source_snapshot_is_rechecked_at_claim_and_storage_refuses_symlinks(self) -> None:
        source = self.project / "source.txt"
        source.write_text("before", encoding="utf-8")
        snapshot = importlib.import_module("orchestra_kit.fingerprint").fingerprint_paths(self.project, ["source.txt"])
        self.submit([self.node("stale", source_snapshot=snapshot)], "stale")
        source.write_text("after", encoding="utf-8")
        self.assertIsNone(self.queue.claim("stale", "alice"))
        task = self.queue.inspect("stale")["tasks"][0]
        self.assertEqual(task["status"], "blocked")
        self.assertTrue(task["block_reason"].startswith("source_stale:"))
        outside = Path(self.temp.name) / "outside.sqlite"
        (self.project / ".orchestra" / "queue.sqlite").unlink()
        (self.project / ".orchestra" / "queue.sqlite").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.module.Queue(self.project, KIT)
        (self.project / ".orchestra" / "queue.sqlite").unlink()
        (self.project / ".orchestra" / "queue.sqlite-wal").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "sidecars"):
            self.module.Queue(self.project, KIT)

    def test_fresh_evidence_policy_requires_a_snapshot_before_claim(self) -> None:
        self.config.write_text(self.config.read_text().replace("require_fresh_evidence = false", "require_fresh_evidence = true"), encoding="utf-8")
        self.submit([self.node("unsnapshotted")])
        self.assertIsNone(self.queue.claim("graph", "alice"))
        task = self.queue.inspect("graph")["tasks"][0]
        self.assertEqual(task["block_reason"], "source_snapshot_required")
