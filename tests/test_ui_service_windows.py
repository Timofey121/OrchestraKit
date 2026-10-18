from __future__ import annotations

import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

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
            data = xml_path.read_bytes()
            self.assertTrue(data.startswith(b"\xff\xfe"))
            self.task_xml = data.decode("utf-16")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "/run" in lowered:
            self.running = True
            paths = service._paths(self.home)
            paths.pid.write_text("4321\n", encoding="ascii")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "/change" in lowered:
            root = ET.fromstring(self.task_xml)
            ns = {"t": service.TASK_NAMESPACE}
            settings = root.find("t:Settings", ns)
            enabled = settings.find("t:Enabled", ns)
            if enabled is None:
                enabled = ET.SubElement(settings, f"{{{service.TASK_NAMESPACE}}}Enabled")
            enabled.text = "false"
            self.task_xml = ET.tostring(root, encoding="unicode")
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

    def native_export(self, trigger_user=r"TESTDOMAIN\runner"):
        """Mirror the normalized XML exported by Windows Server 2022 CI."""
        root = ET.fromstring(service._paths(self.home).task_xml.read_bytes())
        ns = {"t": service.TASK_NAMESPACE}

        def value(path):
            found = root.findtext(path, namespaces=ns)
            self.assertIsNotNone(found, path)
            return escape(found)

        sid = value("t:Principals/t:Principal/t:UserId")
        task_name = escape(service._task_name(sid))
        return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{service.TASK_NAMESPACE}">
  <RegistrationInfo>
    <Description>{escape(service.DESCRIPTION)}</Description>
    <URI>{task_name}</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="OrchestraKitUser">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure>
      <Count>255</Count>
      <Interval>PT1M</Interval>
    </RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
  </Settings>
  <Triggers>
    <LogonTrigger>
      <UserId>{escape(trigger_user)}</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{service.RECOVERY_START_BOUNDARY}</StartBoundary>
    </TimeTrigger>
  </Triggers>
  <Actions Context="OrchestraKitUser">
    <Exec>
      <Command>{value("t:Actions/t:Exec/t:Command")}</Command>
      <Arguments>{value("t:Actions/t:Exec/t:Arguments")}</Arguments>
      <WorkingDirectory>{value("t:Actions/t:Exec/t:WorkingDirectory")}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'''

    def test_install_registers_per_user_logon_task_and_same_pid_runner(self):
        result = self.install(port=9876)

        self.assertTrue(result["installed"])
        self.assertTrue(result["running"])
        self.assertTrue(result["healthy"])
        task_data = Path(result["task_xml"]).read_bytes()
        self.assertTrue(task_data.startswith(b"\xff\xfe"))
        declaration = task_data.decode("utf-16").splitlines()[0]
        self.assertIn("encoding=", declaration)
        self.assertIn("utf-16", declaration)
        root = ET.fromstring(task_data)
        ns = {"t": service.TASK_NAMESPACE}
        self.assertEqual(root.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=ns),
                         "S-1-5-21-123-456-789-1001")
        self.assertEqual(root.findtext("t:Triggers/t:TimeTrigger/t:StartBoundary", namespaces=ns),
                         service.RECOVERY_START_BOUNDARY)
        self.assertEqual(root.findtext(
            "t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", namespaces=ns), "PT1M")
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
            self.assertIn(repr(argument), runner)
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
        self.assertLess(operations.index("/change"), operations.index("/end"))
        self.assertLess(operations.index("/end"), operations.index("/delete"))
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
        root = ET.fromstring(self.task_xml)
        self.assertEqual(root.findtext(
            "t:Settings/t:Enabled", namespaces={"t": service.TASK_NAMESPACE}), "false")

        # A retry recognizes only the precisely-owned disabled state and can
        # finish cleanup without re-enabling its periodic trigger.
        result = service.uninstall_service(home=self.home)
        self.assertFalse(result["installed"])
        self.assertIsNone(self.task_xml)

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
        original_runner = runner.read_bytes()
        runner.write_bytes(original_runner + b"# personal edit\n")
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "modified|ownership"):
            service.service_status(home=self.home)
        with self.assertRaisesRegex(ValueError, "modified|ownership"):
            service.uninstall_service(home=self.home)
        self.assertEqual(self.calls, [])

        # Restore a valid local installation, then change the scheduler copy.
        runner.write_bytes(original_runner)
        self.task_xml = self.task_xml.replace("LeastPrivilege", "HighestAvailable")
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "registered task.*modified"):
            service.uninstall_service(home=self.home)
        self.assertFalse(any(part.lower() in ("/end", "/delete") for call in self.calls for part in call))

    def test_registered_task_with_extra_trigger_is_foreign_and_is_not_ended(self):
        self.install()
        self.task_xml = self.task_xml.replace(
            "<Triggers>",
            "<Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger>",
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

    def test_missing_registered_task_refuses_live_pid_or_occupied_port_before_recovery(self):
        first = self.install()
        paths = service._paths(self.home)
        self.task_xml = None
        self.calls.clear()

        with self.assertRaisesRegex(ValueError, "recorded PID.*active|process.*active"):
            self.install()
        self.assertTrue(Path(first["runner"]).exists())
        self.assertFalse(any("/create" in [part.lower() for part in call] for call in self.calls))

        self.running = False
        self.calls.clear()
        with patch.object(service, "_listening", return_value=True):
            with self.assertRaisesRegex(ValueError, "occupied"):
                self.install()
        self.assertFalse(any("/create" in [part.lower() for part in call] for call in self.calls))

        self.calls.clear()
        recovered = self.install()
        self.assertTrue(recovered["healthy"])
        self.assertTrue(any("/create" in [part.lower() for part in call] for call in self.calls))
        self.assertEqual(paths.pid.read_text(encoding="ascii"), "4321\n")

    def test_missing_registered_task_uninstall_preserves_live_pid_but_cleans_dead_owner(self):
        first = self.install()
        paths = service._paths(self.home)
        self.task_xml = None
        self.calls.clear()

        with self.assertRaisesRegex(ValueError, "recorded PID.*active|process.*active"):
            service.uninstall_service(home=self.home)
        for path in (paths.task_xml, paths.runner, paths.manifest, paths.pid):
            self.assertTrue(path.exists(), path)
        self.assertFalse(any(part.lower() in ("/end", "/delete")
                             for call in self.calls for part in call))

        self.running = False
        result = service.uninstall_service(home=self.home)
        self.assertFalse(result["installed"])
        for path in (paths.task_xml, paths.runner, paths.manifest, paths.pid):
            self.assertFalse(path.exists(), path)

    def test_every_generated_behavior_field_is_part_of_registered_ownership(self):
        self.install()
        original = self.task_xml
        mutations = {
            "hard termination": ("<AllowHardTerminate>true</AllowHardTerminate>",
                                 "<AllowHardTerminate>false</AllowHardTerminate>"),
            "network": ("<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>",
                        "<RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>"),
            "hidden": ("<Hidden>false</Hidden>", "<Hidden>true</Hidden>"),
            "idle": ("<RunOnlyIfIdle>false</RunOnlyIfIdle>",
                     "<RunOnlyIfIdle>true</RunOnlyIfIdle>"),
            "wake": ("<WakeToRun>false</WakeToRun>", "<WakeToRun>true</WakeToRun>"),
            "priority": ("<Priority>7</Priority>", "<Priority>1</Priority>"),
        }
        for name, (before, after) in mutations.items():
            with self.subTest(name=name):
                self.task_xml = original.replace(before, after)
                self.calls.clear()
                with self.assertRaisesRegex(ValueError, "registered task.*modified"):
                    service.uninstall_service(home=self.home)
                self.assertFalse(any(part.lower() in ("/end", "/delete")
                                     for call in self.calls for part in call))
        self.task_xml = original

    def test_extra_trigger_principal_and_settings_constraints_are_foreign(self):
        self.install()
        original = self.task_xml
        additions = {
            "start boundary": ("</LogonTrigger>",
                               "<StartBoundary>2026-01-01T00:00:00</StartBoundary></LogonTrigger>"),
            "end boundary": ("</LogonTrigger>",
                             "<EndBoundary>2030-01-01T00:00:00</EndBoundary></LogonTrigger>"),
            "delay": ("</LogonTrigger>", "<Delay>PT1M</Delay></LogonTrigger>"),
            "trigger execution limit": ("</LogonTrigger>",
                                        "<ExecutionTimeLimit>PT1M</ExecutionTimeLimit>"
                                        "</LogonTrigger>"),
            "repetition": ("</LogonTrigger>",
                           "<Repetition><Interval>PT1M</Interval></Repetition></LogonTrigger>"),
            "privilege": ("</Principal>",
                          "<RequiredPrivileges><Privilege>SeDebugPrivilege</Privilege>"
                          "</RequiredPrivileges></Principal>"),
            "network profile": ("</Settings>",
                                "<NetworkProfileName>foreign</NetworkProfileName></Settings>"),
        }
        for name, (before, after) in additions.items():
            with self.subTest(name=name):
                self.task_xml = original.replace(before, after, 1)
                self.calls.clear()
                with self.assertRaisesRegex(ValueError, "registered task.*modified"):
                    service.uninstall_service(home=self.home)
                self.assertFalse(any(part.lower() in ("/end", "/delete")
                                     for call in self.calls for part in call))
        self.task_xml = original

    def test_periodic_recovery_trigger_is_strictly_owned(self):
        self.install()
        original = self.task_xml
        mutations = {
            "interval": ("<Interval>PT1M</Interval>", "<Interval>PT2M</Interval>"),
            "boundary": (service.RECOVERY_START_BOUNDARY, "2001-01-01T00:00:00"),
            "duration": ("</Repetition>", "<Duration>PT2M</Duration></Repetition>"),
            "stop": ("</Repetition>",
                     "<StopAtDurationEnd>true</StopAtDurationEnd></Repetition>"),
            "disabled": ("</TimeTrigger>", "<Enabled>false</Enabled></TimeTrigger>"),
        }
        for name, (before, after) in mutations.items():
            with self.subTest(name=name):
                self.task_xml = original.replace(before, after, 1)
                self.calls.clear()
                with self.assertRaisesRegex(ValueError, "registered task.*modified"):
                    service.uninstall_service(home=self.home)
                self.assertFalse(any(part.lower() in ("/end", "/delete")
                                     for call in self.calls for part in call))
        self.task_xml = original

    def test_documented_scheduler_defaults_are_safe_registration_normalization(self):
        result = self.install()
        source = Path(result["task_xml"]).read_bytes().decode("utf-16")
        normalized = source.replace(
            "</LogonTrigger>",
            "<ExecutionTimeLimit>PT72H</ExecutionTimeLimit><Delay>PT0M</Delay></LogonTrigger>",
        ).replace(
            "</Principal>", "<ProcessTokenSidType>None</ProcessTokenSidType></Principal>", 1
        ).replace(
            "</Settings>",
            "<IdleSettings><Duration>PT10M</Duration><WaitTimeout>PT1H</WaitTimeout>"
            "<StopOnIdleEnd>true</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle>"
            "</IdleSettings><DeleteExpiredTaskAfter>PT0S</DeleteExpiredTaskAfter>"
            "<UseUnifiedSchedulingEngine>false</UseUnifiedSchedulingEngine>"
            "<DisallowStartOnRemoteAppSession>false</DisallowStartOnRemoteAppSession>"
            "<Volatile>false</Volatile></Settings>",
        )
        self.assertEqual(service._task_signature(normalized), service._task_signature(source))

    def test_native_scheduler_export_omissions_and_account_alias_are_normalized(self):
        result = self.install()
        expected = service._task_signature(Path(result["task_xml"]).read_bytes())
        self.task_xml = self.native_export()

        with patch.object(service, "_account_name_for_sid",
                          return_value=r"TESTDOMAIN\runner") as account_name:
            self.assertEqual(service._task_signature(self.task_xml), expected)
            status = service.service_status(home=self.home)

        self.assertTrue(status["installed"])
        self.assertTrue(status["healthy"])
        account_name.assert_called_with("S-1-5-21-123-456-789-1001")

    def test_native_scheduler_export_account_alias_must_resolve_to_owner_sid(self):
        self.install()
        self.task_xml = self.native_export()
        self.calls.clear()

        with patch.object(service, "_account_name_for_sid",
                          return_value=r"TESTDOMAIN\different-user"):
            with self.assertRaisesRegex(ValueError, "registered task.*modified"):
                service.uninstall_service(home=self.home)

        self.assertFalse(any(part.lower() in ("/end", "/delete")
                             for call in self.calls for part in call))

    def test_native_scheduler_export_does_not_normalize_elevated_or_extra_privileges(self):
        self.install()
        native = self.native_export()
        mutations = {
            "elevated": ("</LogonType>",
                         "</LogonType><RunLevel>HighestAvailable</RunLevel>"),
            "extra privilege": ("</LogonType>",
                                "</LogonType><RequiredPrivileges>"
                                "<Privilege>SeDebugPrivilege</Privilege>"
                                "</RequiredPrivileges>"),
        }
        for name, (before, after) in mutations.items():
            with self.subTest(name=name):
                self.task_xml = native.replace(before, after, 1)
                self.calls.clear()
                with patch.object(service, "_account_name_for_sid",
                                  return_value=r"TESTDOMAIN\runner"):
                    with self.assertRaisesRegex(ValueError, "registered task.*modified"):
                        service.uninstall_service(home=self.home)
                self.assertFalse(any(part.lower() in ("/end", "/delete")
                                     for call in self.calls for part in call))

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
