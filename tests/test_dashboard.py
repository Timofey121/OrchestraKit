from __future__ import annotations

import http.client
import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


KIT = Path(__file__).resolve().parents[1]


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / ".orchestra" / "runs").mkdir(parents=True)
        (self.project / ".orchestra" / "project.toml").write_text(
            (KIT / "templates" / "project.toml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.codex = self.root / "codex"
        self.codex.mkdir()

    def dashboard(self):
        module = importlib.import_module("orchestra_kit.dashboard")
        return module.Dashboard([self.project], self.codex, KIT)

    def _task(self, task_id: str = "11111111-1111-1111-1111-111111111111") -> None:
        payload = {
            "generated_by": "OrchestraKit", "task_id": task_id, "revision": 2,
            "goal": "Make the dashboard", "status": "running", "updated_at": "2026-10-05T10:00:00+00:00",
            "history": [], "metrics": {"input_tokens": None, "output_tokens": None,
                                      "elapsed_seconds": None, "cost_usd": None},
            "last_result": {"summary": "working", "completed_steps": ["test"], "next_steps": ["ship"],
                            "changed_files": ["src/a.py"], "checks": [], "reviews": []},
        }
        (self.project / ".orchestra" / "runs" / (task_id + ".json")).write_text(json.dumps(payload), encoding="utf-8")

    def _task_for_chat(self, task_id: str, chat_id: str, status: str = "running") -> None:
        payload = {
            "generated_by": "OrchestraKit", "task_id": task_id, "revision": 1,
            "goal": "Bound work", "status": status, "chat_id": chat_id,
            "updated_at": "2026-10-05T10:00:00+00:00", "history": [], "metrics": {},
            "last_result": {"checks": [], "reviews": []},
        }
        (self.project / ".orchestra" / "runs" / f"{task_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    def _execution_for_task(self, execution_id: str, task_id: str, state: str = "launch") -> None:
        execution = self.project / ".orchestra" / "executions" / execution_id
        execution.mkdir(parents=True)
        (execution / "run.json").write_text(json.dumps({
            "execution_uuid": execution_id, "task_id": task_id, "state": state,
            "timestamp": "2026-10-05T10:00:00+00:00", "attempts": [],
        }), encoding="utf-8")

    def _threads(self) -> None:
        db = self.codex / "state_5.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, model TEXT, reasoning_effort TEXT, updated_at TEXT, archived INTEGER, project_id TEXT, rollout_path TEXT)")
            conn.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)", ("chat-a", "Alpha", str(self.project), "gpt-x", "high", "2026-10-05", 0, "project-a", None))
            conn.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)", ("chat-b", "Other", str(self.root / "other"), "gpt-y", "low", "2026-10-04", 0, None, None))

    def test_snapshot_and_detail_are_bounded_and_match_project_chats(self) -> None:
        self._task()
        self._threads()
        dashboard = self.dashboard()
        overview = dashboard.snapshot()
        self.assertEqual(overview["totals"]["projects"], 1)
        self.assertEqual(overview["totals"]["tasks"], 1)
        self.assertEqual(overview["chats"][0]["id"], "chat-a")
        self.assertEqual(overview["chats"][0]["status"], "unknown")
        self.assertEqual(overview["projects"][0]["counts"]["persisted_running_tasks"], 1)
        self.assertEqual(overview["projects"][0]["counts"]["live_chats"], 0)
        self.assertIsNone(overview["chats"][0].get("usage"))
        detail = dashboard.project_details(overview["projects"][0]["id"])
        self.assertEqual(detail["tasks"][0]["id"], "11111111-1111-1111-1111-111111111111")
        self.assertIsNone(detail["tasks"][0]["metrics"]["input_tokens"])
        self.assertEqual([chat["id"] for chat in detail["chats"]], ["chat-a"])

    def test_multiple_explicit_projects_keep_chats_with_their_own_cwd(self) -> None:
        second = self.root / "second"
        (second / ".orchestra").mkdir(parents=True)
        (second / ".orchestra" / "project.toml").write_text(
            (KIT / "templates" / "project.toml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        db = self.codex / "state_5.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, model TEXT, reasoning_effort TEXT, updated_at TEXT, archived INTEGER, project_id TEXT)")
            conn.execute("INSERT INTO threads VALUES ('one', 'One', ?, 'm1', 'low', 'new', 0, NULL)", (str(self.project),))
            conn.execute("INSERT INTO threads VALUES ('two', 'Two', ?, 'm2', 'high', 'new', 0, NULL)", (str(second),))
        dashboard = importlib.import_module("orchestra_kit.dashboard").Dashboard([self.project, second], self.codex, KIT)
        overview = dashboard.snapshot()
        self.assertEqual(overview["totals"]["projects"], 2)
        self.assertEqual({chat["project_id"] for chat in overview["chats"]}, {project["id"] for project in overview["projects"]})

    def test_chat_filter_uses_only_explicit_task_and_execution_ids(self) -> None:
        first_task = "11111111-1111-1111-1111-111111111111"
        second_task = "22222222-2222-2222-2222-222222222222"
        self._task_for_chat(first_task, "chat-a")
        self._task_for_chat(second_task, "chat-b", "completed")
        self._execution_for_task("exec-a", first_task)
        self._execution_for_task("exec-b", second_task, "terminal")
        with sqlite3.connect(self.codex / "state_5.sqlite") as conn:
            conn.execute("CREATE TABLE threads (id TEXT, name TEXT, cwd TEXT, source TEXT, updated_at INTEGER, archived INTEGER)")
            conn.execute("INSERT INTO threads VALUES ('chat-a', 'Alpha', ?, 'vscode', 2, 0)", (str(self.project),))
            conn.execute("INSERT INTO threads VALUES ('chat-b', 'Beta', ?, 'vscode', 1, 0)", (str(self.project),))
        dashboard = self.dashboard()
        project_id = dashboard.snapshot()["projects"][0]["id"]
        detail = dashboard.project_details(project_id, chat_id="chat-a")
        self.assertEqual([item["id"] for item in detail["tasks"]], [first_task])
        self.assertEqual([item["id"] for item in detail["executions"]], ["exec-a"])
        self.assertEqual([item["id"] for item in detail["chats"]], ["chat-a"])
        self.assertEqual(detail["filter"], {"chat_id": "chat-a"})
        chat = detail["chats"][0]
        self.assertEqual(chat["task_ids"], [first_task])
        self.assertEqual(chat["execution_ids"], ["exec-a"])
        self.assertEqual(chat["counts"]["persisted_running_tasks"], 1)
        self.assertEqual(chat["counts"]["nonterminal_executions"], 1)
        with self.assertRaises(KeyError):
            dashboard.project_details(project_id, chat_id="missing")

    def test_registered_database_project_is_discovered_and_thread_project_id_wins(self) -> None:
        registered = self.root / "registered"
        registered.mkdir()
        worktree = self.root / "worktree"
        (worktree / ".orchestra").mkdir(parents=True)
        (worktree / ".orchestra" / "project.toml").write_text(
            (KIT / "templates" / "project.toml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        with sqlite3.connect(self.codex / "state_5.sqlite") as conn:
            conn.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT);
                CREATE TABLE threads (id TEXT, name TEXT, cwd TEXT, source TEXT, updated_at INTEGER,
                                      archived INTEGER, project_id TEXT, agent_path TEXT);
            """)
            conn.execute("INSERT INTO projects VALUES ('native-project', 'Native')")
            conn.execute("INSERT INTO project_roots VALUES ('native-project', 0, ?)", (str(registered),))
            conn.execute("INSERT INTO threads VALUES ('registered-chat', 'Registered', ?, 'vscode', 2, 0, 'native-project', NULL)", (str(self.root / "elsewhere"),))
            conn.execute("INSERT INTO threads VALUES ('worktree-chat', 'Worktree', ?, 'vscode', 3, 0, 'native-project', NULL)", (str(worktree),))
            conn.execute("INSERT INTO threads VALUES ('guardian', 'Guardian', ?, '{\"subagent\":{\"other\":\"guardian\"}}', 4, 0, 'native-project', NULL)", (str(registered),))
        dashboard = importlib.import_module("orchestra_kit.dashboard").Dashboard([], self.codex, KIT)
        overview = dashboard.snapshot()
        projects = {item["path"]: item for item in overview["projects"]}
        self.assertIn(str(registered), projects)
        self.assertFalse(projects[str(registered)]["configured"])
        self.assertIn(str(worktree), projects)
        by_chat = {chat["id"]: chat["project_id"] for chat in overview["chats"]}
        self.assertEqual(by_chat["registered-chat"], projects[str(registered)]["id"])
        self.assertEqual(by_chat["worktree-chat"], projects[str(worktree)]["id"])
        self.assertNotIn("guardian", by_chat)

    def test_global_assignments_group_chats_and_codex_name_wins_project_config(self) -> None:
        replica = self.project
        config = replica / ".orchestra" / "project.toml"
        config.write_text(config.read_text(encoding="utf-8").replace(
            'name = "__PROJECT_NAME__"', 'name = "AbitCall"'), encoding="utf-8")
        general = self.root / "general"
        general.mkdir()
        empty = self.root / "empty-project"
        empty.mkdir()
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        with sqlite3.connect(self.codex / "state_5.sqlite") as conn:
            conn.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, position INTEGER);
                CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT);
                CREATE TABLE threads (id TEXT, name TEXT, cwd TEXT, source TEXT, updated_at INTEGER,
                                      archived INTEGER, project_id TEXT);
            """)
            conn.executemany("INSERT INTO projects VALUES (?,?,?)", (
                ("native-replica", "replika", 0),
                ("native-general", "general", 1),
                ("native-empty", "Empty", 2),
            ))
            conn.executemany("INSERT INTO project_roots VALUES (?,?,?)", (
                ("native-replica", 0, str(replica)),
                ("native-general", 0, str(general)),
                ("native-empty", 0, str(empty)),
            ))
            conn.execute("INSERT INTO threads VALUES ('replica-chat', 'Replica chat', ?, 'vscode', 2, 0, NULL)",
                         (str(unrelated),))
            conn.execute("INSERT INTO threads VALUES ('general-chat', 'General chat', ?, 'vscode', 1, 0, NULL)",
                         (str(unrelated),))
        (self.codex / ".codex-global-state.json").write_text(json.dumps({
            "local-projects": {
                "legacy-replica": {"name": "replika", "rootPaths": [str(replica)]},
                "legacy-general": {"name": "general", "rootPaths": [str(general)]},
            },
            "thread-project-assignments": {
                "replica-chat": {"projectKind": "local", "projectId": "legacy-replica"},
                "general-chat": {"projectKind": "local", "projectId": "legacy-general"},
            },
        }), encoding="utf-8")

        overview = self.dashboard().snapshot()
        projects = {item["name"]: item for item in overview["projects"]}
        self.assertEqual(set(projects), {"replika", "general", "Empty"})
        self.assertTrue(projects["replika"]["configured"])
        self.assertFalse(projects["Empty"]["configured"])
        self.assertEqual(projects["Empty"]["counts"]["chats"], 0)
        chat_projects = {chat["id"]: chat["project_id"] for chat in overview["chats"]}
        self.assertEqual(chat_projects["replica-chat"], projects["replika"]["id"])
        self.assertEqual(chat_projects["general-chat"], projects["general"]["id"])

    def test_stale_registered_roots_do_not_hide_a_later_valid_project(self) -> None:
        registered = self.root / "registered-after-stale"
        registered.mkdir()
        with sqlite3.connect(self.codex / "state_5.sqlite") as conn:
            conn.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, position INTEGER);
                CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT);
            """)
            for index in range(100):
                project_id = f"stale-{index}"
                conn.execute("INSERT INTO projects VALUES (?,?,?)", (project_id, project_id, index))
                conn.execute("INSERT INTO project_roots VALUES (?,?,?)", (
                    project_id, 0, str(self.root / "missing" / project_id)))
            conn.execute("INSERT INTO projects VALUES ('valid', 'Valid registered', 100)")
            conn.execute("INSERT INTO project_roots VALUES ('valid', 0, ?)", (str(registered),))

        overview = importlib.import_module("orchestra_kit.dashboard").Dashboard([], self.codex, KIT).snapshot()
        self.assertEqual([(item["name"], item["path"]) for item in overview["projects"]],
                         [("Valid registered", str(registered))])

    def test_latest_database_project_name_wins_over_older_state(self) -> None:
        registered = self.root / "renamed-project"
        registered.mkdir()
        for version, name in ((4, "old-name"), (5, "replika")):
            with sqlite3.connect(self.codex / f"state_{version}.sqlite") as conn:
                conn.executescript("""
                    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, position INTEGER);
                    CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT);
                """)
                conn.execute("INSERT INTO projects VALUES ('project-id', ?, 0)", (name,))
                conn.execute("INSERT INTO project_roots VALUES ('project-id', 0, ?)", (str(registered),))

        overview = importlib.import_module("orchestra_kit.dashboard").Dashboard([], self.codex, KIT).snapshot()
        self.assertEqual([(item["name"], item["path"]) for item in overview["projects"]],
                         [("replika", str(registered))])

    def test_latest_database_moved_root_wins_and_keeps_current_multi_roots(self) -> None:
        old = self.root / "old-root"
        primary = self.root / "new-primary"
        secondary = self.root / "new-secondary"
        unrelated = self.root / "unrelated"
        for path in (old, primary, secondary, unrelated):
            path.mkdir()
        with sqlite3.connect(self.codex / "state_4.sqlite") as conn:
            conn.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, position INTEGER);
                CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT);
            """)
            conn.execute("INSERT INTO projects VALUES ('project-id', 'Old', 0)")
            conn.execute("INSERT INTO project_roots VALUES ('project-id', 0, ?)", (str(old),))
        with sqlite3.connect(self.codex / "state_5.sqlite") as conn:
            conn.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, position INTEGER);
                CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT);
                CREATE TABLE threads (id TEXT, name TEXT, cwd TEXT, source TEXT, updated_at INTEGER,
                                      archived INTEGER, project_id TEXT);
            """)
            conn.execute("INSERT INTO projects VALUES ('project-id', 'Current', 0)")
            conn.execute("INSERT INTO project_roots VALUES ('project-id', 0, ?)", (str(primary),))
            conn.execute("INSERT INTO project_roots VALUES ('project-id', 1, ?)", (str(secondary),))
            conn.execute("INSERT INTO threads VALUES ('chat', 'Moved chat', ?, 'vscode', 1, 0, 'project-id')",
                         (str(unrelated),))

        overview = importlib.import_module("orchestra_kit.dashboard").Dashboard([], self.codex, KIT).snapshot()
        projects = {item["path"]: item for item in overview["projects"]}
        self.assertEqual(set(projects), {str(primary), str(secondary)})
        chat = next(item for item in overview["chats"] if item["id"] == "chat")
        self.assertEqual(chat["project_id"], projects[str(primary)]["id"])

    def test_uses_recent_threads_from_the_latest_state_and_normalizes_numeric_time(self) -> None:
        for version, title in ((4, "old database"), (5, "new database")):
            with sqlite3.connect(self.codex / f"state_{version}.sqlite") as conn:
                conn.execute("CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, updated_at INTEGER, archived INTEGER)")
                for index in range(210):
                    conn.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (f"old-{index}", "old", str(self.project), index, 0))
                conn.execute("INSERT INTO threads VALUES (?,?,?,?,?)", ("current", title, str(self.project), 1_759_657_600, 0))
        chats = self.dashboard().snapshot()["chats"]
        current = next(chat for chat in chats if chat["id"] == "current")
        self.assertEqual(current["title"], "new database")
        self.assertTrue(current["updated_at"].endswith("+00:00"))
        self.assertNotIn("old-0", {chat["id"] for chat in chats})

    def test_reads_large_global_state_and_uses_registered_project_name_for_known_root(self) -> None:
        payload = {"padding": "x" * (300 * 1024), "local-projects": {"p": {"name": "replika", "rootPaths": [str(self.project)]}}}
        (self.codex / ".codex-global-state.json").write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.dashboard().snapshot()["projects"][0]["name"], "replika")

    def test_chat_name_and_non_guardian_child_workspace_survive_the_cap(self) -> None:
        child = self.project / "nested"
        child.mkdir()
        with sqlite3.connect(self.codex / "state_5.sqlite") as conn:
            conn.execute("CREATE TABLE threads (id TEXT, name TEXT, title TEXT, cwd TEXT, source TEXT, updated_at INTEGER, archived INTEGER)")
            for index in range(250):
                conn.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", (f"guardian-{index}", "guardian", "prompt", str(self.project), '{\"subagent\":{\"other\":\"guardian\"}}', 10_000 + index, 0))
                conn.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", (f"exec-{index}", "exec", "prompt", str(self.project), "exec", 20_000 + index, 0))
            conn.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", ("root", "Изучить видео и Jeff", "very long initial prompt", str(child), "vscode", 1, 0))
        overview = self.dashboard().snapshot()
        self.assertEqual([chat["id"] for chat in overview["chats"]], ["root"])
        self.assertEqual(overview["chats"][0]["title"], "Изучить видео и Jeff")

    def test_completed_task_derives_current_then_stale_verification(self) -> None:
        subject = self.project / "subject.txt"
        subject.write_text("before", encoding="utf-8")
        evidence = importlib.import_module("orchestra_kit.evidence").run_evidence(
            self.project, ["subject.txt"], [{"argv": [sys.executable, "-c", "pass"], "failure_cause": "implementation"}]
        )
        task_id = "33333333-3333-3333-3333-333333333333"
        payload = {"generated_by": "OrchestraKit", "task_id": task_id, "revision": 1, "goal": "Verify",
                   "status": "completed", "updated_at": "2026-10-05", "history": [],
                   "metrics": {}, "last_result": {"changed_files": ["subject.txt"],
                   "checks": [{"required": True, "evidence_ref": evidence["evidence_ref"]}], "reviews": []}}
        (self.project / ".orchestra" / "runs" / (task_id + ".json")).write_text(json.dumps(payload), encoding="utf-8")
        dashboard = self.dashboard()
        overview = dashboard.snapshot(refresh=True)
        detail = dashboard.project_details(overview["projects"][0]["id"], refresh=True)
        self.assertEqual(detail["tasks"][0]["verification_state"], "current")
        subject.write_text("after", encoding="utf-8")
        self.assertEqual(dashboard.project_details(overview["projects"][0]["id"], refresh=True)["tasks"][0]["verification_state"], "stale")

    def test_live_standard_events_reset_activity_and_refresh_bypasses_short_cache(self) -> None:
        self._threads()
        sessions = self.codex / "sessions"
        sessions.mkdir()
        rollout = sessions / "chat-a.jsonl"
        rollout.write_text("\n".join(json.dumps(event) for event in (
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "First task"}]}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
            {"type": "event_msg", "payload": {"type": "turn_context", "model": "current-model", "reasoning_effort": "max"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
        )) + "\n", encoding="utf-8")
        with sqlite3.connect(self.codex / "state_5.sqlite") as conn:
            conn.execute("UPDATE threads SET rollout_path=? WHERE id='chat-a'", (str(rollout),))
        dashboard = self.dashboard()
        first = dashboard.snapshot()["chats"][0]
        self.assertEqual(first["status"], "running")
        self.assertNotIn("last_activity", first)
        self.assertEqual((first["model"], first["effort"]), ("current-model", "max"))
        rollout.write_text(rollout.read_text(encoding="utf-8") + json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n", encoding="utf-8")
        self.assertEqual(dashboard.snapshot()["chats"][0]["status"], "running")
        self.assertEqual(dashboard.snapshot(refresh=True)["chats"][0]["status"], "idle")

    def test_descriptive_commentary_survives_generic_tool_event_and_resets_next_turn(self):
        self._threads()
        sessions=self.codex/'sessions';sessions.mkdir()
        rollout=sessions/'chat-a.jsonl'
        events=[{'type':'turn_context','payload':{'model':'gpt-current','effort':'ultra'}},
                {'type':'event_msg','payload':{'type':'task_started'}},
                {'type':'response_item','payload':{'type':'message','role':'assistant','phase':'commentary','content':[{'type':'output_text','text':'Проверяю тесты панели'}]}},
                {'type':'event_msg','payload':{'type':'item_completed','item':{'type':'CommandExecution'}}}]
        rollout.write_text('\n'.join(json.dumps(e) for e in events)+'\n')
        with sqlite3.connect(self.codex/'state_5.sqlite') as db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id='chat-a'",(str(rollout),))
        chat=self.dashboard().snapshot()['chats'][0]
        self.assertEqual((chat['model'],chat['effort']),('gpt-current','ultra'))
        self.assertEqual(chat['last_activity'],'Проверяю тесты панели')
        self.assertEqual(chat['status'],'running')
        events.extend([{'type':'event_msg','payload':{'type':'task_complete'}},{'type':'event_msg','payload':{'type':'task_started'}}])
        rollout.write_text('\n'.join(json.dumps(e) for e in events)+'\n')
        self.assertNotIn('last_activity',self.dashboard().snapshot()['chats'][0])

    def test_truncated_rollout_only_infers_running_from_fresh_activity(self) -> None:
        self._threads()
        sessions = self.codex / "sessions"
        sessions.mkdir()
        rollout = sessions / "chat-a.jsonl"
        oversized = "x" * (2 * 1024 * 1024 + 32)
        activity = {"type": "event_msg", "timestamp": "2026-10-05T10:00:00+00:00", "payload": {
            "type": "item_completed", "item": {"type": "CommandExecution"}}}
        rollout.write_text(oversized + "\n" + json.dumps(activity) + "\n", encoding="utf-8")
        with sqlite3.connect(self.codex / "state_5.sqlite") as conn:
            conn.execute("UPDATE threads SET rollout_path=? WHERE id='chat-a'", (str(rollout),))
        old = 1_700_000_000
        import os
        os.utime(rollout, (old, old))
        self.assertEqual(self.dashboard().snapshot()["chats"][0]["status"], "unknown")
        os.utime(rollout, None)
        self.assertEqual(self.dashboard().snapshot()["chats"][0]["status"], "running")

    def test_stale_started_rollout_is_unknown_but_terminal_is_authoritative(self) -> None:
        self._threads()
        sessions = self.codex / "sessions"
        sessions.mkdir()
        rollout = sessions / "chat-a.jsonl"
        rollout.write_text(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n", encoding="utf-8")
        with sqlite3.connect(self.codex / "state_5.sqlite") as conn:
            conn.execute("UPDATE threads SET rollout_path=? WHERE id='chat-a'", (str(rollout),))
        import os
        os.utime(rollout, (1_700_000_000, 1_700_000_000))
        self.assertEqual(self.dashboard().snapshot()["chats"][0]["status"], "unknown")
        rollout.write_text(rollout.read_text(encoding="utf-8") + json.dumps(
            {"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n", encoding="utf-8")
        os.utime(rollout, (1_700_000_000, 1_700_000_000))
        self.assertEqual(self.dashboard().snapshot()["chats"][0]["status"], "idle")

    def test_long_prompt_title_uses_concise_non_prompt_fallback(self) -> None:
        with sqlite3.connect(self.codex / "state_5.sqlite") as conn:
            conn.execute("CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, source TEXT, updated_at INTEGER, archived INTEGER)")
            conn.execute("INSERT INTO threads VALUES ('chat-prompt', ?, ?, 'vscode', 1, 0)",
                         ("# Files mentioned by the user:\n\n## secret.txt\n" + "request " * 100, str(self.project)))
        chat = self.dashboard().snapshot()["chats"][0]
        self.assertEqual(chat["title"], "Chat chat-pro")
        self.assertNotIn("Files mentioned", chat["title"])

    def test_orchestra_symlink_is_not_followed(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.project / ".orchestra").rename(self.project / ".orchestra-real")
        (self.project / ".orchestra").symlink_to(outside, target_is_directory=True)
        overview = self.dashboard().snapshot()
        self.assertEqual(overview["totals"]["tasks"], 0)
        self.assertFalse(overview["projects"][0]["configured"])

    def test_missing_state_database_is_read_only_and_corrupt_artifacts_warn(self) -> None:
        self._task("22222222-2222-2222-2222-222222222222")
        (self.project / ".orchestra" / "runs" / "bad.json").write_text("not-json", encoding="utf-8")
        dashboard = self.dashboard()
        self.assertFalse((self.codex / "state_5.sqlite").exists())
        overview = dashboard.snapshot()
        self.assertFalse((self.codex / "state_5.sqlite").exists())
        self.assertTrue(overview["warnings"])
        self.assertEqual(overview["totals"]["tasks"], 1)

    def test_execution_history_keeps_latest_runs_before_applying_limit(self) -> None:
        import os
        from unittest.mock import patch

        directories = []
        for index in range(101):
            execution_id = f"exec-{index:03}"
            self._execution_for_task(execution_id, "task")
            directory = self.project / ".orchestra" / "executions" / execution_id
            os.utime(directory, (1_700_000_000 + index, 1_700_000_000 + index))
            directories.append(directory)
        root = directories[0].parent
        original = Path.iterdir
        # Filesystem enumeration has no chronological ordering guarantee.
        with patch.object(Path, "iterdir", lambda path: iter(directories) if path.resolve() == root.resolve() else original(path)):
            dashboard = self.dashboard()
            project_id = dashboard.snapshot()["projects"][0]["id"]
            records = dashboard.project_details(project_id)["executions"]
        ids = {record["id"] for record in records}
        self.assertEqual(len(records), 100)
        self.assertIn("exec-100", ids)
        self.assertNotIn("exec-000", ids)

    def test_native_run_events_queue_and_live_chat_are_read_without_mutation(self) -> None:
        self._task()
        execution = self.project / ".orchestra" / "executions" / "exec-1"
        execution.mkdir(parents=True)
        (execution / "run.json").write_text(json.dumps({"state": "launch", "timestamp": "now", "attempts": []}), encoding="utf-8")
        (execution / "events.jsonl").write_text(json.dumps({"task_id": "11111111-1111-1111-1111-111111111111", "phase": "launch", "timestamp": "now", "attempt": 1}) + "\n", encoding="utf-8")
        queue = self.project / ".orchestra" / "queue.sqlite"
        with sqlite3.connect(queue) as conn:
            conn.executescript("CREATE TABLE tasks (graph_id TEXT, node_id TEXT, status TEXT, block_reason TEXT, lease_expires REAL, launches INTEGER, observed_tokens INTEGER); CREATE TABLE dependencies (graph_id TEXT, node_id TEXT, depends_on TEXT);")
            conn.execute("INSERT INTO tasks VALUES ('g', 'n', 'running', NULL, 1.0, 2, NULL)")
        self._threads()
        sessions = self.codex / "sessions"
        sessions.mkdir()
        rollout = sessions / "chat-a.jsonl"
        rollout.write_text(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n" + json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "phase": "commentary", "content": "Testing dashboard"}}}) + "\n", encoding="utf-8")
        with sqlite3.connect(self.codex / "state_5.sqlite") as conn:
            conn.execute("UPDATE threads SET rollout_path=? WHERE id='chat-a'", (str(rollout),))
        overview = self.dashboard().snapshot()
        detail = self.dashboard().project_details(overview["projects"][0]["id"])
        self.assertEqual(detail["executions"][0]["status"], "launch")
        self.assertEqual(detail["executions"][0]["events"][0]["phase"], "launch")
        self.assertEqual(detail["queue"][0]["node_id"], "n")
        self.assertEqual(detail["chats"][0]["status"], "running")
        self.assertEqual(detail["chats"][0]["last_activity"], "Testing dashboard")
        rollout.write_text(rollout.read_text(encoding="utf-8") + json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n", encoding="utf-8")
        self.assertEqual(self.dashboard().snapshot()["chats"][0]["status"], "idle")

    def test_http_rejects_hosts_origins_posts_and_path_escapes(self) -> None:
        module = importlib.import_module("orchestra_kit.dashboard")
        static = self.root / "static"
        static.mkdir()
        (static / "app.js").write_text("console.log('ok')", encoding="utf-8")
        (static / "room.png").write_bytes(b"\x89PNG\r\n\x1a\nimage")
        outside = self.root / "outside"
        outside.write_text("no", encoding="utf-8")
        (static / "style.css").symlink_to(outside)
        server = module.make_server(self.dashboard(), static_root=static)
        self.addCleanup(server.server_close)
        import threading
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        def request(method: str, path: str, **headers: str):
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            body = response.read()
            conn.close()
            return response, body
        response, _ = request("GET", "/api/overview", Host="127.0.0.1:" + str(port))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertIn("img-src 'self' data:", response.getheader("Content-Security-Policy"))
        image, body = request("GET", "/room.png", Host="127.0.0.1:" + str(port))
        self.assertEqual((image.status, image.getheader("Content-Type"), body), (200, "image/png", b"\x89PNG\r\n\x1a\nimage"))
        self.assertEqual(request("GET", "/api/overview", Host="evil.test")[0].status, 421)
        self.assertEqual(request("GET", "/api/overview", Host="127.0.0.1:" + str(port), Origin="http://evil.test")[0].status, 403)
        self.assertEqual(request("POST", "/api/overview", Host="127.0.0.1:" + str(port))[0].status, 405)
        self.assertEqual(request("GET", "/../outside", Host="127.0.0.1:" + str(port))[0].status, 404)
        self.assertEqual(request("GET", "/room.png/../outside", Host="127.0.0.1:" + str(port))[0].status, 404)
        self.assertEqual(request("GET", "/style.css", Host="127.0.0.1:" + str(port))[0].status, 404)
