from pathlib import Path
import json,tempfile,unittest
from orchestra_kit.config import load_config,ConfigError

KIT=Path(__file__).resolve().parents[1]
class RepairContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.project=Path(self.tmp.name).resolve();(self.project/'.orchestra').mkdir()
        self.config=self.project/'.orchestra/project.toml'
        self.template=(KIT/'templates/project.toml').read_text()
        self.config.write_text(self.template)
        self.log=self.project/'.orchestra/checks/run/fail.log';self.log.parent.mkdir(parents=True)
        self.log.write_text('Traceback\nKeyError: missing\n'+'INFO idle\n'*500)
        self.checks=[{'argv':['python3','task.py'],'failure_cause':'implementation','returncode':1,'log':str(self.log)}]
    def test_mode_is_project_owned_and_invalid_mode_is_refused(self):
        self.assertEqual(load_config(self.project,KIT).context.repair_context,'tail')
        self.config.write_text(self.template.replace('[context]','[context]\nrepair_context = "diagnostic"'))
        self.assertEqual(load_config(self.project,KIT).context.repair_context,'diagnostic')
        self.config.write_text(self.template.replace('[context]','[context]\nrepair_context = "unsafe"'))
        with self.assertRaises(ConfigError):load_config(self.project,KIT)
    def test_diagnostic_retry_retains_early_error_and_criteria(self):
        from orchestra_kit.execution import _repair_evidence
        packet=_repair_evidence(self.project,self.checks,'implementation',strategy='diagnostic',max_chars=2400)
        self.assertIn('KeyError: missing',packet)
        self.assertIn('original criteria remain binding',packet)
        decoded=json.loads(packet.split('\n',2)[2]);self.assertTrue(decoded['original_criteria_remain_binding'])
        self.assertEqual(decoded['failures'][0]['argv'],['python3','task.py'])
    def test_tail_strategy_and_undersized_budget_are_explicit(self):
        from orchestra_kit.execution import _repair_evidence
        text=_repair_evidence(self.project,self.checks,'implementation',strategy='tail',max_chars=2400)
        self.assertNotIn('KeyError: missing',text);self.assertIn('INFO idle',text)
        with self.assertRaises(ValueError):_repair_evidence(self.project,self.checks,'implementation',strategy='diagnostic',max_chars=10)
