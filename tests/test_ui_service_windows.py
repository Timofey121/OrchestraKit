from __future__ import annotations

import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from orchestra_kit import ui_service_windows as service


class UiServiceWindowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.skill = self.home / "skills with spaces/orchestra"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "scripts/orchestra.py").write_text("# launcher\n", encoding="utf-8")
        self.codex = self.home / "custom codex"
        self.calls: list[list[str]] = []
        self.task_xml: str | None = None
        self.running = False
        self.platform = patch.object(service.sys, "platform", "win32")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        for name, value in [
            ("_current_user_sid", "S-1-5-21-123-456-789-1001"),
            ("_listening", False),
            ("_healthy", True),
        ]:
            mock = patch.object(service, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(service, "_pid_running", side_effect=lambda pid: self.running)
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(service.subprocess, "run", side_effect=self.schtasks)
        mock.start()
        self.addCleanup(mock.stop)

    def schtasks(self, argv, **kwargs):
        self.calls.append(argv)
        self.assertEqual(argv[0], "schtasks.exe")
        self.assertFalse(kwargs.get("shell", False))
        self.assertLessEqual(kwargs["timeout"], 10)
        lowered = [part.lower() for part in argv]
        if "/query" in lowered:
            if "/tn" not in lowered:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if self.task_xml is None:
                return subprocess.CompletedProcess(argv, service.TASK_NOT_FOUND_HRESULT,
                                                   "", "missing")
            return subprocess.CompletedProcess(argv, 0, self.task_xml, "")
        if "/create" in lowered:
            xml_path = Path(argv[lowered.index("/xml") + 1])
            self.task_xml = xml_path.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "/run" in lowered:
            self.running = True
            paths = service._paths(self.home)
            paths.pid.write_text("4321\n", encoding="ascii")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "/end" in lowered:
            self.running = False
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "/delete" in lowered:
            self.task_xml = None
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.fail(f"unexpected schtasks invocation: {argv}")

    def install(self, **kwargs):
        return service.install_service(self.skill, self.codex, home=self.home, **kwargs)

    def test_install_registers_per_user_logon_task_and_same_pid_runner(self):
        result = self.install(port=9876)

        self.assertTrue(result["installed"])
        self.assertTrue(result["running"])
        self.assertTrue(result["healthy"])
        root = ET.fromstring(Path(result["task_xml"]).read_text(encoding="utf-8"))
        ns = {"t": service.TASK_NAMESPACE}
        self.assertEqual(root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=ns),
                         "S-1-5-21-123-456-789-1001")
        self.assertEqual(root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns),
                         "InteractiveToken")
        self.assertEqual(root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns),
                         "LeastPrivilege")
        self.assertEqual(root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=ns),
                         "IgnoreNew")
        self.assertEqual(root.findtext("t:Settings/t:RestartOnFailure/t:Interval", namespaces=ns),
                         "PT1M")
        self.assertEqual(root.findtext("t:Settings/t:RestartOnFailure/t:Count", namespaces=ns), "255")
        command = root.findtext("t:Actions/t:Exec/t:Command", namespaces=ns)
        arguments = root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
        self.assertEqual(command, str(service._task_python()))
        self.assertIn(str(Path(result["runner"])), arguments)
        runner = Path(result["runner"]).read_text(encoding="utf-8")
        self.assertIn("runpy.run_path", runner)
        self.assertIn(repr(str(self.skill / "scripts/orchestra.py")), runner)
        for argument in ("--all-projects", "--no-browser", "--host", "127.0.0.1",
                         "--port", "9876", "--codex-home", str(self.codex)):
            self.assertIn(argument, runner)
        operations = [next(part for part in call if part.startswith("/")) for call in self.calls]
        self.assertIn("/create", operations)
        self.assertIn("/run", operations)
        create = next(call for call in self.calls if "/create" in [part.lower() for part in call])
        self.assertNotIn("/f", [part.lower() for part in create])

    def test_repeated_install_does_not_replace_or_duplicate_task(self):
        first = self.install()
        original = Path(first["task_xml"]).read_bytes()
        self.calls.clear()

        second = self.install()

        self.assertEqual(Path(second["task_xml"]).read_bytes(), original)
        self.assertFalse(any("/create" in [part.lower() for part in call] for call in self.calls))
        self.assertFalse(any("/run" in [part.lower() for part in call] for call in self.calls))

    def test_uninstall_ends_and_deletes_only_validated_owned_task_and_preserves_log(self):
        first = self.install()
        log = Path(first["log"])
        log.write_text("diagnostics", encoding="utf-8")
        self.calls.clear()

        result = service.uninstall_service(home=self.home)

        self.assertFalse(result["installed"])
        self.assertEqual(log.read_text(encoding="utf-8"), "diagnostics")
        self.assertFalse(Path(first["task_xml"]).exists())
        self.assertFalse(Path(first["runner"]).exists())
        operations = [part.lower() for call in self.calls for part in call]
        self.assertIn("/end", operations)
        self.assertIn("/delete", operations)

    def test_uninstall_preserves_task_and_files_when_owned_process_cannot_be_ended(self):
        first = self.install()
        original = self.schtasks

        def fail_end(argv, **kwargs):
            if "/end" in [part.lower() for part in argv]:
                return subprocess.CompletedProcess(argv, 0x80070005, "", "access denied")
            return original(argv, **kwargs)

        with patch.object(service.subprocess, "run", side_effect=fail_end):
            with self.assertRaisesRegex(ValueError, "end failed.*access denied"):
                service.uninstall_service(home=self.home)

        self.assertIsNotNone(self.task_xml)
        self.assertTrue(Path(first["task_xml"]).exists())
        self.assertTrue(Path(first["runner"]).exists())

    def test_existing_foreign_task_or_port_is_never_taken_over(self):
        self.task_xml = "<Task>foreign</Task>"
        with self.assertRaisesRegex(ValueError, "ownership|already exists"):
            self.install()
        self.assertFalse(any("/create" in [part.lower() for part in call] for call in self.calls))

        self.task_xml = None
        self.calls.clear()
        with patch.object(service, "_listening", return_value=True):
            with self.assertRaisesRegex(ValueError, "occupied"):
                self.install()
        self.assertFalse(any("/create" in [part.lower() for part in call] for call in self.calls))

    def test_local_or_registered_tampering_blocks_status_install_and_uninstall(self):
        first = self.install()
        runner = Path(first["runner"])
        original_runner = runner.read_text(encoding="utf-8")
        runner.write_text(original_runner + "# personal edit\n", encoding="utf-8")
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "modified|ownership"):
            service.service_status(home=self.home)
        with self.assertRaisesRegex(ValueError, "modified|ownership"):
            service.uninstall_service(home=self.home)
        self.assertEqual(self.calls, [])

        # Restore a valid local installation, then change the scheduler copy.
        runner.write_text(original_runner, encoding="utf-8")
        self.task_xml = self.task_xml.replace("LeastPrivilege", "HighestAvailable")
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "registered task.*modified"):
            service.uninstall_service(home=self.home)
        self.assertFalse(any(part.lower() in ("/end", "/delete") for call in self.calls for part in call))

    def test_registered_task_with_extra_trigger_is_foreign_and_is_not_ended(self):
        self.install()
        self.task_xml = self.task_xml.replace(
            "<Triggers>",
            "<Triggers><TimeTrigger><StartBoundary>2026-01-01T00:00:00</StartBoundary>"
            "<Enabled>true</Enabled></TimeTrigger>",
        )
        self.calls.clear()

        with self.assertRaisesRegex(ValueError, "registered task.*modified"):
            service.uninstall_service(home=self.home)

        self.assertFalse(any(part.lower() in ("/end", "/delete") for call in self.calls for part in call))

    def test_absent_task_and_unavailable_scheduler_are_distinct(self):
        self.assertEqual(service.service_status(home=self.home), {
            "supported": True, "installed": False, "running": False, "healthy": False
        })
        with patch.object(service.subprocess, "run", side_effect=FileNotFoundError("schtasks.exe")):
            with self.assertRaisesRegex(ValueError, "Task Scheduler.*unavailable"):
                service.service_status(home=self.home)
        with patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["schtasks.exe"], 0x80070005, "", "access denied")):
            with self.assertRaisesRegex(ValueError, "Task Scheduler.*unavailable"):
                service.service_status(home=self.home)
        with patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["schtasks.exe"], 0x8004130F, "", "account information not set")):
            with self.assertRaisesRegex(ValueError, "Task Scheduler.*unavailable"):
                service.service_status(home=self.home)

    def test_failed_immediate_start_leaves_recoverable_owned_installation(self):
        original = self.schtasks

        def fail_run(argv, **kwargs):
            if "/run" in [part.lower() for part in argv]:
                return subprocess.CompletedProcess(argv, 5, "", "start rejected")
            return original(argv, **kwargs)

        with patch.object(service.subprocess, "run", side_effect=fail_run):
            with self.assertRaisesRegex(ValueError, "start rejected"):
                self.install()
        self.assertIsNotNone(self.task_xml)
        service.uninstall_service(home=self.home)
        self.assertIsNone(self.task_xml)

    def test_health_requires_pid_file_process_and_matching_dashboard_pid(self):
        first = self.install()
        self.calls.clear()
        with patch.object(service, "_healthy", return_value=False) as healthy:
            status = service.service_status(home=self.home)
        self.assertTrue(status["running"])
        self.assertFalse(status["healthy"])
        self.assertEqual(status["pid"], 4321)
        healthy.assert_called_once_with(8731, 4321)

        service._paths(self.home).pid.write_text("not-a-pid", encoding="ascii")
        with patch.object(service, "_healthy") as healthy:
            status = service.service_status(home=self.home)
        self.assertFalse(status["running"])
        self.assertFalse(status["healthy"])
        healthy.assert_not_called()

    def test_symlinked_config_path_invalid_port_and_unsupported_platform_are_refused(self):
        appdata = self.home / "AppData/Local"
        appdata.mkdir(parents=True)
        other = self.home / "other"
        other.mkdir()
        (appdata / "OrchestraKit").symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.install()
        self.assertEqual(list(other.iterdir()), [])

        (appdata / "OrchestraKit").unlink()
        for port in (0, 65536, True):
            with self.assertRaisesRegex(ValueError, "port"):
                self.install(port=port)
        with patch.object(service.sys, "platform", "linux"):
            self.assertFalse(service.service_status(home=self.home)["supported"])
            with self.assertRaisesRegex(ValueError, "Windows"):
                self.install()

    def test_scheduler_timeout_has_clear_capability_error(self):
        with patch.object(service.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("schtasks.exe", 10)):
            with self.assertRaisesRegex(ValueError, "Task Scheduler.*timed out"):
                self.install()

    def test_utf16le_scheduler_xml_without_bom_is_decoded(self):
        source = '<?xml version="1.0" encoding="utf-16"?><Task>проверка</Task>'
        self.assertEqual(service._decode_output(source.encode("utf-16-le")), source)


if __name__ == "__main__":
    unittest.main()
