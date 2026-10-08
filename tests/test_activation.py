from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


class ActivationAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("orchestra_kit.activation"))
        from orchestra_kit.activation import audit_activation

        self.audit = audit_activation
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.skill = Path("/opt/skills/orchestra/SKILL.md")

    def log(self, name: str, rows: list[dict] | list[str]) -> Path:
        path = self.root / name
        path.write_text("".join(row if isinstance(row, str) else json.dumps(row) + "\n" for row in rows))
        return path

    def native_call(self, call_id: str, command: str, event_id="call-event") -> dict:
        return {"type": "response_item", "payload": {
            "type": "custom_tool_call", "id": event_id, "call_id": call_id,
            "name": "exec", "input": (
                "const r = await tools.exec_command({" + json.dumps("cmd") + ":" + json.dumps(command) + "});"
            )}}

    def native_output(self, call_id: str, text: object, event_id="output-event") -> dict:
        return {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "id": event_id, "call_id": call_id,
            "output": text}}

    def test_native_read_requires_associated_tool_output(self) -> None:
        command = "sed -n '1,240p' /opt/skills/orchestra/SKILL.md"
        path = self.log("native.jsonl", [self.native_call("read", command)])
        result = self.audit([path], self.skill)
        self.assertEqual(result["attempted"][0]["event_id"], "call-event")
        self.assertEqual(result["observed"], [])
        self.assertEqual(result["successful"], [])
        self.assertTrue(any("no associated output" in warning for warning in result["warnings"]))

    def test_yaml_hash_in_name_and_multiline_shell_cannot_pass(self) -> None:
        for name, command, header in [
            ('hash', f'cat {self.skill}', '---\nname: orchestra#impostor\n'),
            ('newline', f'cat {self.skill}\nprintf header', '---\nname: orchestra\n')]:
            with self.subTest(name=name):
                path = self.log(name + '.jsonl', [{'type': 'item.completed', 'item': {
                    'id': name, 'type': 'command_execution', 'command': command,
                    'exit_code': 0, 'aggregated_output': header}}])
                result = self.audit([path], self.skill)
                self.assertNotEqual(result['status'], 'pass')
                self.assertEqual(result['successful'], [])

    def test_normal_skill_prose_does_not_look_truncated_and_nested_shell_is_uncertain(self) -> None:
        header = '---\nname: orchestra\n---\nAfter a truncated read, retrieve missing content.\n'
        rows = [{'type': 'item.completed', 'item': {'id': 'normal', 'type': 'command_execution',
                 'command': f'cat {self.skill}', 'exit_code': 0, 'aggregated_output': header}}]
        result = self.audit([self.log('normal-prose.jsonl', rows)], self.skill)
        self.assertEqual(result['successful'][0]['read_scope'], 'full')
        rows[0]['item']['command'] = f"sh -c 'cat {self.skill} ; printf header'"
        result = self.audit([self.log('nested-shell.jsonl', rows)], self.skill)
        self.assertNotEqual(result['status'], 'pass')

    def test_native_completed_header_is_observed_not_exit_attested(self) -> None:
        command = "sed -n '1,240p' /opt/skills/orchestra/SKILL.md"
        output = "Script completed\nOutput:\n---\nname: orchestra\n"
        path = self.log("native.jsonl", [self.native_call("read", command), self.native_output("read", output)])
        result = self.audit([path], self.skill)
        self.assertEqual(result["successful"], [])
        self.assertEqual(result["observed"][0]["evidence_level"], "observed_header")
        self.assertEqual(result["observed"][0]["read_scope"], "partial: lines 1-240")
        self.assertTrue(any("not exit-attested" in warning for warning in result["warnings"]))
        self.assertNotIn(output, repr(result))

    def test_native_script_completed_without_exact_skill_header_is_not_observed(self) -> None:
        command = f"cat {self.skill}"
        rows = [
            self.native_call("empty", command, "empty"),
            self.native_output("empty", "Script completed\nOutput:\n", "empty-output"),
            self.native_call("wrong-name", command, "wrong-name"),
            self.native_output("wrong-name", "Script completed\nOutput:\n---\nname: another-skill\n", "wrong-output"),
        ]
        result = self.audit([self.log("native-negative.jsonl", rows)], self.skill)
        self.assertEqual(result["observed"], [])
        self.assertEqual(result["successful"], [])

    def test_native_string_output_honors_child_exit_and_downgrades_truncated_full_read(self) -> None:
        command = f"cat {self.skill}"
        wrapped = json.dumps({"exit_code": 0, "output": "Script completed\nOutput:\n---\nname: orchestra\nWarning: truncated output\n"})
        path = self.log("native-string.jsonl", [self.native_call("read", command),
            self.native_output("read", wrapped)])
        result = self.audit([path], self.skill)
        self.assertEqual(result["observed"][0]["read_scope"], "partial: output truncated")
        self.assertEqual(result["successful"][0]["evidence_level"], "exit_attested")

    def test_batched_native_js_literals_match_ordered_child_results(self) -> None:
        source = (
            'text(await tools.exec_command({cmd:"cat /opt/skills/orchestra/SKILL.md",max_output_tokens:12000})); '
            'text(await tools.exec_command({cmd:"pwd; rg --files",max_output_tokens:4000}));'
        )
        call = {"type": "response_item", "payload": {"type": "custom_tool_call", "id": "batched",
                "call_id": "batch", "name": "exec", "input": source}}
        output = {"type": "response_item", "payload": {"type": "custom_tool_call_output",
                  "id": "batch-output", "call_id": "batch", "output": [
                      {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
                      {"type": "input_text", "text": json.dumps({"exit_code": 0,
                          "output": "---\nname: orchestra\n"})},
                      {"type": "input_text", "text": json.dumps({"exit_code": 1,
                          "output": "working directory only"})},
                  ]}}
        result = self.audit([self.log("batched.jsonl", [call, output])], self.skill)
        self.assertEqual(result["status"], "pass")
        self.assertEqual([row["event_id"] for row in result["successful"]], ["batched"])

    def test_unsupported_native_js_with_target_path_is_unknown(self) -> None:
        source = "tools.exec_command({cmd: build_command('/opt/skills/orchestra/SKILL.md')})"
        call = {"type": "response_item", "payload": {"type": "custom_tool_call", "id": "dynamic",
                "call_id": "dynamic", "name": "exec", "input": source}}
        result = self.audit([self.log("dynamic.jsonl", [call])], self.skill)
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(any("unsupported JavaScript" in warning for warning in result["warnings"]))

    def test_impostor_yaml_name_and_compound_cli_read_cannot_pass(self) -> None:
        command = f"cat {self.skill}; printf ignored"
        row = {"type": "item.completed", "item": {"id": "compound", "type": "command_execution",
               "command": command, "exit_code": 0,
               "aggregated_output": "---\nname: orchestra impostor\n"}}
        result = self.audit([self.log("compound.jsonl", [row])], self.skill)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["successful"], [])

    def test_parallel_native_calls_are_not_order_attested(self) -> None:
        source = ('await Promise.all([tools.exec_command({cmd:"cat /opt/skills/orchestra/SKILL.md"}), '
                  'tools.exec_command({cmd:"pwd"})])')
        call = {"type": "response_item", "payload": {"type": "custom_tool_call", "id": "parallel",
                "call_id": "parallel", "name": "exec", "input": source}}
        output = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "id": "parallel-out",
                  "call_id": "parallel", "output": [
                      {"type": "input_text", "text": json.dumps({"exit_code": 0,
                          "output": "---\nname: orchestra\n"})},
                      {"type": "input_text", "text": json.dumps({"exit_code": 0, "output": "cwd"})},
                  ]}}
        result = self.audit([self.log("parallel.jsonl", [call, output])], self.skill)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["successful"], [])

    def test_agent_messages_path_lookalikes_and_wc_do_not_count_as_reads(self) -> None:
        path = self.log("negative.jsonl", [
            {"type": "response_item", "payload": {"type": "message", "content": str(self.skill)}},
            self.native_call("wc", f"wc -l {self.skill}"),
            self.native_call("wrong", "cat /opt/skills/orchestra/SKILL.md.bak"),
            self.native_output("wc", "Script completed\nOutput:\n7\n"),
            self.native_output("wrong", "Script completed\nOutput:\n---\nname: orchestra\n"),
        ])
        result = self.audit([path], self.skill)
        self.assertEqual(result["attempted"], [])
        self.assertEqual(result["successful"], [])

    def test_cli_success_requires_exit_code_and_output_header(self) -> None:
        command = f"cat {self.skill}"
        failed = {"type": "item.completed", "item": {"id": "failed", "type": "command_execution",
                  "command": command, "exit_code": 1, "aggregated_output": "Script completed\nOutput:\n"}}
        no_header = {"type": "item.completed", "item": {"id": "no-header", "type": "command_execution",
                     "command": command, "exit_code": 0, "aggregated_output": "command output omitted"}}
        passed = {"type": "item.completed", "item": {"id": "passed", "type": "command_execution",
                  "command": command, "exit_code": 0,
                  "aggregated_output": "Script completed\nOutput:\n---\nname: orchestra\n"}}
        result = self.audit([self.log("cli.jsonl", [failed, no_header, passed])], self.skill)
        self.assertEqual([row["event_id"] for row in result["attempted"]], ["failed", "no-header", "passed"])
        self.assertEqual([row["event_id"] for row in result["successful"]], ["passed"])
        self.assertEqual(result["successful"][0]["evidence_level"], "exit_attested")

    def test_configured_model_is_metadata_not_runtime_attestation_and_partial_is_unknown(self) -> None:
        path = self.log("partial.jsonl", [
            {"type": "turn_context", "payload": {"model": "example", "effort": "high"}},
            self.native_call("read", f"cat {self.skill}"),
            "{broken",
        ])
        result = self.audit([path], self.skill)
        self.assertFalse(result["complete"])
        self.assertEqual(result["configured_models"], [{"model": "example", "effort": "high",
                                                          "source": "turn_context (configured, not runtime-attested)"}])
        self.assertEqual(result["status"], "unknown")

    def test_general_skill_duplicates_and_public_activities_are_summarized(self) -> None:
        other = "/opt/skills/humanizer/SKILL.md"
        rows = [
            self.native_call("a", f"sed -n '1,100p' {other}", "a"),
            self.native_output("a", "Script completed\nOutput:\n---\nname: humanizer\n", "ao"),
            self.native_call("b", f"sed -n '50,150p' {other}", "b"),
            self.native_output("b", "Script completed\nOutput:\n---\nname: humanizer\n", "bo"),
            self.native_call("script", "python3 tool.py --check", "script"),
            self.native_output("script", "Script completed\nOutput:\nok", "so"),
        ]
        result = self.audit([self.log("activities.jsonl", rows)], self.skill)
        duplicate = result["skill_read_duplicates"][0]
        self.assertEqual(duplicate["path"], other)
        self.assertEqual(duplicate["event_ids"], ["a", "b"])
        self.assertEqual(result["activities"]["scripts"][0]["event_id"], "script")


if __name__ == "__main__":
    unittest.main()
