from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orchestra_kit.config import load_config
from orchestra_kit.project import init_project
from orchestra_kit.routing import build_brief

KIT = Path(__file__).resolve().parents[1]


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        init_project(self.project, KIT)
        (self.project / 'app.py').write_text('value = 1\n')
        self.task = {
            'recipe': 'bugfix', 'goal': 'Preserve signed zero: -0.00',
            'files': ['app.py', 'test_app.py'], 'scope': 'Only app.py and test_app.py; preserve API.',
            'acceptance': ['Accept -0.00', 'Reject negative nonzero values'],
            'checks': [{'argv': [sys.executable, '-c', 'pass'], 'failure_cause': 'implementation'}],
            'context': {'facts': ['Original callers need Decimal']},
        }

    def recipes(self):
        self.assertIsNotNone(importlib.util.find_spec('orchestra_kit.recipes'), 'recipe compiler is missing')
        return importlib.import_module('orchestra_kit.recipes')

    def test_compiler_preserves_requirements_and_does_not_mutate_or_dispatch(self):
        recipes = self.recipes()
        original = copy.deepcopy(self.task)
        for kind in ('bugfix', 'feature', 'refactor'):
            with self.subTest(kind=kind):
                task = {**self.task, 'recipe': kind}
                job = recipes.compile_recipe(self.project, KIT, task)
                self.assertEqual(job['recipe'], kind)
                self.assertEqual(job['brief']['GOAL'], task['goal'])
                self.assertEqual(job['brief']['ACCEPTANCE CRITERIA'], task['acceptance'])
                self.assertEqual(job['brief']['SCOPE'], task['scope'])
                self.assertEqual(job['brief']['CONTEXT'], task['context'])
                self.assertEqual(job['checks'], task['checks'])
                self.assertEqual(job['evidence_files'], task['files'])
                self.assertEqual(job['size'], 'small')
                self.assertFalse(job['independent'])
                self.assertNotIn('app.py', job['brief']['VERIFICATION'])  # no file-content duplication
                job['brief']['ACCEPTANCE CRITERIA'].append('child mutation')
                job['checks'][0]['argv'].append('child mutation')
        self.assertEqual(self.task, original)
        self.assertFalse((self.project / '.orchestra/executions').exists())
        self.assertFalse((self.project / '.orchestra/workflows').exists())

    def test_worker_and_reviewer_get_only_the_selected_recipe(self):
        recipes = self.recipes()
        config = load_config(self.project, KIT)
        for kind in ('bugfix', 'feature', 'refactor'):
            job = recipes.compile_recipe(self.project, KIT, {**self.task, 'recipe': kind})
            for role in ('worker', 'reviewer'):
                job['role'] = role
                rendered = recipes.render_recipe_brief(config, job)
                expected = recipes.recipe_instructions(kind, role)
                self.assertEqual(rendered.count(expected), 1)
                self.assertIn('Preserve signed zero', rendered)
                self.assertEqual(rendered.count('ACCEPTANCE CRITERIA'), 1)
                for other in ('bugfix', 'feature', 'refactor'):
                    if other != kind:
                        self.assertNotIn(recipes.recipe_instructions(other, role), rendered)
                self.assertLessEqual(len(expected), 600)

    def test_legacy_prompt_is_byte_identical_without_recipe(self):
        recipes = self.recipes()
        job = recipes.compile_recipe(self.project, KIT, self.task)
        job.pop('recipe')
        config = load_config(self.project, KIT)
        self.assertEqual(recipes.render_recipe_brief(config, job), build_brief(config, job['brief']))

    def test_invalid_inputs_are_refused_before_any_execution(self):
        recipes = self.recipes()
        invalid = [
            {'recipe': 'auto'}, {'recipe': None}, {'recipe': []}, {'goal': ''},
            {'scope': ''}, {'acceptance': []}, {'acceptance': ['']}, {'files': []},
            {'files': ['../outside.py']}, {'files': [str(self.project / 'app.py')]},
            {'files': ['.orchestra']}, {'files': ['app.py', 'app.py']},
            {'checks': []}, {'role': 'explorer'}, {'independent': 1}, {'size': 'giant'},
            {'complexity': 'unknown'}, {'risk': 'unknown'}, {'max_calls': 100},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                recipes.compile_recipe(self.project, KIT, {**self.task, **changes})
        for key in ('goal', 'scope', 'acceptance', 'files', 'checks', 'recipe'):
            task = dict(self.task)
            del task[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                recipes.compile_recipe(self.project, KIT, task)
        self.assertFalse((self.project / '.orchestra/executions').exists())

    def test_recipe_text_counts_against_the_brief_budget(self):
        recipes = self.recipes()
        config = load_config(self.project, KIT)
        job = recipes.compile_recipe(self.project, KIT, self.task)
        job['brief']['CONTEXT'] = 'x'
        size = len(build_brief(config, job['brief']))
        job['brief']['CONTEXT'] += 'x' * (config.context.max_brief_chars - size)
        self.assertEqual(len(build_brief(config, job['brief'])), config.context.max_brief_chars)
        with self.assertRaisesRegex(ValueError, 'budget'):
            recipes.render_recipe_brief(config, job)

    def test_explicit_routing_uses_project_policy_without_recipe_strength_override(self):
        recipes = self.recipes()
        from orchestra_kit.routing import route_task
        config = load_config(self.project, KIT)
        for kind in ('bugfix', 'feature', 'refactor'):
            job = recipes.compile_recipe(self.project, KIT, {
                **self.task, 'recipe': kind, 'complexity': 'complex', 'risk': 'high',
                'size': 'substantial', 'independent': True})
            route = route_task(config, **{key: job[key] for key in ('role', 'complexity', 'risk', 'size', 'independent')})
            self.assertEqual(route['profile'], 'strong')
            self.assertEqual(route['execution'], 'leaf')

    def test_cli_compiles_job_that_existing_workflow_accepts_without_model_calls(self):
        task = self.project / 'task.json'
        task.write_text(json.dumps({**self.task, 'size': 'substantial', 'independent': True}))
        command = [sys.executable, '-m', 'orchestra_kit']
        compiled = subprocess.run(command + ['recipe', str(self.project), '--input', str(task)], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        job = self.project / 'job.json'
        job.write_text(compiled.stdout)
        result = subprocess.run(command + ['workflow', str(self.project), '--input', str(job), '--max-launches', '6', '--dry-run'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'dry-run')
        self.assertFalse((self.project / '.orchestra/executions').exists())
        self.assertFalse((self.project / '.orchestra/workflows').exists())

    def test_describe_needs_no_project_and_returns_only_selected_steps(self):
        for kind in ('bugfix', 'feature', 'refactor'):
            result = subprocess.run([sys.executable, '-m', 'orchestra_kit', 'recipe',
                str(self.project / 'not-a-project'), '--describe', kind], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value['recipe'], kind)
            self.assertEqual(set(value), {'recipe', 'worker', 'reviewer'})
            self.assertEqual(value['worker'], self.recipes().recipe_instructions(kind, 'worker'))
        self.assertFalse((self.project / 'not-a-project').exists())

    def test_three_recipes_pass_real_graders_with_same_dispatch_count_as_general_flow(self):
        # The subprocess substitutes for a model; filesystem checks and evidence are real.
        from orchestra_kit.evaluation_cases import CASES
        from orchestra_kit.evidence import verify_evidence
        from orchestra_kit.workflow import run_workflow
        recipes = self.recipes()
        cases = {'bugfix': 'expense-csv-decimals', 'feature': 'ttl-cache-bounds',
                 'refactor': 'sales-ledger-refactor'}
        for kind, name in cases.items():
            case = CASES[name]
            for selected in (False, True):
                with self.subTest(kind=kind, selected=selected):
                    root = self.project / f'{kind}-{selected}'
                    root.mkdir()
                    init_project(root, KIT)
                    for path, text in case['files'].items():
                        target = root / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(text)
                    grader = self.project / f'{kind}-grader.py'
                    grader.write_text(case['tests'])
                    check = [sys.executable, '-I', '-B', str(grader), str(root)]
                    initial = subprocess.run(check, capture_output=True, text=True)
                    self.assertNotEqual(initial.returncode, 0, 'unfinished starter unexpectedly met all target criteria')
                    baseline_command = None
                    if kind == 'refactor':
                        # Target checks also require a new reusable class; separately pin legacy outputs.
                        probe = (
                            'import json,runpy,sys; f=runpy.run_path(sys.argv[1])["summarize_sales"]; '
                            'print(json.dumps([f([]),f([{"region":"west","amount":"1.335"},'
                            '{"region":"west","amount":"1.335"}]),'
                            'f([{"region":"east","amount":0},{"region":"west","amount":-3}])],sort_keys=True))'
                        )
                        baseline_command = [sys.executable, '-I', '-B', '-c', probe, str(root/'sales.py')]
                        baseline = subprocess.run(baseline_command, capture_output=True, text=True)
                        self.assertEqual(baseline.returncode, 0, baseline.stderr)
                    fake = self.project / f'{kind}-fake.py'
                    fake.write_text(
                        'import json,sys\nfrom pathlib import Path\n'
                        'prompt=sys.stdin.read()\nreview="review_original_goal" in prompt\n'
                        f'solution={case["solution"]!r}\n'
                        'if not review:\n'
                        ' for name,text in solution.items():\n'
                        '  p=Path.cwd()/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)\n'
                        'text="VERDICT: PASS\\nReference solution checked" if review else "Reference applied"\n'
                        'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":text}}))\n'
                        'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2,"cached_input_tokens":0}}))\n'
                    )
                    task = {'recipe': kind, 'goal': case['goal'],
                            'files': sorted(set(case['files']) | set(case['solution'])),
                            'scope': {'write_paths': case['write_paths']},
                            'acceptance': case['acceptance'],
                            'checks': [{'argv': check, 'failure_cause': 'implementation'}],
                            'complexity': 'simple', 'risk': 'low',
                            'size': 'substantial', 'independent': True}
                    task['scope'] = json.dumps(task['scope'])
                    job = recipes.compile_recipe(root, KIT, task)
                    if not selected:
                        job.pop('recipe')
                    result = run_workflow(root, KIT, job, max_calls=2,
                        codex_command=[sys.executable, str(fake)])
                    self.assertEqual(result['status'], 'verified', result)
                    self.assertEqual(result['model_attempts'], 2)
                    self.assertEqual(result['repair_decisions'], 0)
                    self.assertEqual(subprocess.run(check, capture_output=True).returncode, 0)
                    if baseline_command:
                        after = subprocess.run(baseline_command, capture_output=True, text=True)
                        self.assertEqual(after.returncode, 0, after.stderr)
                        self.assertEqual(after.stdout, baseline.stdout)
                    for phase in result['receipts']:
                        verify_evidence(root, phase['receipt']['evidence_ref'],
                            kind='review' if phase['role'] == 'reviewer' else 'check')
