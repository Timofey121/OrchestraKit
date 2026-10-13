from pathlib import Path
import json,os,subprocess,sys,tempfile,unittest

KIT=Path(__file__).resolve().parents[1]
class StageOneCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.log=self.root/'.orchestra/checks/run/check.log'
        self.log.parent.mkdir(parents=True);self.log.write_text('INFO ready\nValueError: invalid input\nINFO stopped\n',encoding='utf-8')
    def run_cli(self,*args):
        return subprocess.run([sys.executable,'-m','orchestra_kit',*args],cwd=KIT,env=dict(os.environ,PYTHONPATH=str(KIT/'src')),capture_output=True,text=True)
    def test_log_search_concise_and_json_refer_to_same_lines(self):
        short=self.run_cli('log','search',str(self.root),'.orchestra/checks/run/check.log','--query','ValueError','--context-lines','0')
        self.assertEqual(short.returncode,0,short.stderr)
        self.assertIn('check.log:2: ValueError: invalid input',short.stdout)
        self.assertNotIn('INFO ready',short.stdout)
        detail=self.run_cli('log','search',str(self.root),'.orchestra/checks/run/check.log','--query','ValueError','--context-lines','0','--json')
        self.assertEqual(detail.returncode,0,detail.stderr)
        self.assertEqual(json.loads(detail.stdout)['lines'][0]['line_number'],2)
    def test_log_read_reports_page_and_refuses_foreign_path(self):
        page=self.run_cli('log','read',str(self.root),'.orchestra/checks/run/check.log','--max-lines','1')
        self.assertEqual(page.returncode,0,page.stderr)
        self.assertIn('next_start_line=2',page.stdout)
        self.assertIn('more=true',page.stdout)
        denied=self.run_cli('log','read',str(self.root),'../outside.log')
        self.assertEqual(denied.returncode,2)
    def test_compare_workflows_accepts_same_profile_for_different_variants(self):
        self.assertEqual(self.run_cli('init',str(self.root)).returncode,0)
        sample=lambda n:{'fixture_fingerprint':'same-task','receipt':{'status':'verified','profile':'cheap','requested_model':'gpt-5.6-luna','requested_effort':'medium','attempts':[{'number':1,'profile':'cheap','requested_model':'gpt-5.6-luna','requested_effort':'medium','usage':{'complete':True,'totals':{'input_tokens':n,'cached_input_tokens':0,'uncached_input_tokens':n,'output_tokens':1,'total_tokens':n+1}}}]},'quality':{'checks_passed':True,'acceptance_passed':True,'review_verdict':'PASS'},'elapsed_seconds':1}
        path=self.root/'samples.json';path.write_text(json.dumps([{'variant':'before','sample':sample(20)},{'variant':'after','sample':sample(10)}]))
        result=self.run_cli('compare-workflows',str(self.root),'--input',str(path),'--profile','cheap','--variant','before','--variant','after','--baseline','before')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['recommendation']['variant'],'after')
