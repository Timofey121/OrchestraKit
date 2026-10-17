"""Reproducible synthetic coding tasks, external graders and observed receipts.

Preparing, grading and reporting are local. Only ``run_suite`` invokes Codex,
through the existing native runner, with a pre-dispatch attempt reservation.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any

from .calibration import _sum_known, _usage, compare_workflows
from .check_execution import run_checks
from .config import load_config
from .execution import run_job
from .workflow import run_workflow
from .project import init_project, sync_project

SCHEMA = 1
_LABEL = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_COVERAGE = 'worker and reviewer calls; excludes development and host-internal unobserved work'


def _cases():
    from .evaluation_cases import CASES
    return CASES


def list_cases() -> list[dict[str, Any]]:
    return [{'id':key,'category':case['category'],'goal':case['goal']} for key,case in _cases().items()]


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _child(root: Path, relative: str) -> Path:
    if not isinstance(relative,str) or not relative or Path(relative).is_absolute() or any(p in {'..','.'} for p in Path(relative).parts):
        raise ValueError('unsafe evaluation path')
    current=root
    for part in Path(relative).parts:
        current=current/part
        if current.is_symlink():raise ValueError('symlinked evaluation path')
    if root.resolve() not in current.resolve().parents:raise ValueError('evaluation path escapes root')
    return current


def _read(path: Path) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size>2_000_000:
        raise ValueError('evaluation record must be a bounded regular file')
    return json.loads(path.read_text(encoding='utf-8'))


def _write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(value,stream,ensure_ascii=False,allow_nan=False,indent=2)
        stream.write('\n')


def _case_fingerprint(case: dict) -> dict:
    return {'schema':SCHEMA,'fixture':_hash({k:case[k] for k in ('category','goal','acceptance','files','tests','write_paths')}),
            'python':list(sys.version_info[:2]),'platform':platform.system()}


def _integration(workspace: Path) -> dict[str,str]:
    names={'AGENTS.md','.orchestra/project.toml','.orchestra/manifest.json'}
    for folder in ('.agents','.codex'):
        directory=_child(workspace,folder)
        if directory.is_symlink():raise ValueError('symlinked integration')
        for path in directory.rglob('*'):
            if path.is_symlink():raise ValueError('symlinked integration')
            if path.is_file():names.add(path.relative_to(workspace).as_posix())
    if len(names)>128:raise ValueError('evaluation integration exceeds file limit')
    result={}
    for name in sorted(names):
        path=_child(workspace,name)
        if not path.is_file() or path.stat().st_size>1_000_000:raise ValueError('invalid evaluation integration file')
        result[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def prepare_suite(output: Path, kit: Path, *, case_ids=None, repeats=2, variant='baseline', repair_cycles=0) -> dict:
    cases=_cases(); selected=list(cases) if case_ids is None else list(case_ids)
    if (not selected or len(set(selected))!=len(selected) or any(key not in cases for key in selected)
            or type(repeats) is not int or not 1<=repeats<=10
            or not isinstance(variant,str) or not _LABEL.fullmatch(variant)
            or type(repair_cycles) is not int or not 0<=repair_cycles<=2):
        raise ValueError('invalid evaluation cases, repeats, variant or repair cycles')
    if output.is_symlink() or output.exists():raise ValueError('evaluation output must be a new directory')
    # Validate every fixture before writing. These are bundled code, never an
    # arbitrary user-supplied command or Python-grader manifest.
    for key in selected:
        if not _LABEL.fullmatch(key):raise ValueError('invalid fixture id')
        case=cases[key]
        if set(case['solution'])-set(case['write_paths']):raise ValueError('solution exceeds write scope')
        for name in [*case['files'],*case['write_paths']]:_child(output.absolute(),name)
    output.mkdir(parents=True,mode=0o700)
    output=output.resolve()
    trials=[]
    for repeat in range(1,repeats+1):
        # Reverse alternate repetitions to reduce systematic order effects.
        for key in selected if repeat%2 else reversed(selected):
            trial=output/f'{key}-{repeat}';workspace=trial/'workspace';workspace.mkdir(parents=True)
            case=cases[key]
            for name,text in case['files'].items():
                path=_child(workspace,name);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8',newline='\n')
            # New output subjects need to exist for native source fingerprints.
            for name in case['write_paths']:
                path=_child(workspace,name)
                if not path.exists():path.parent.mkdir(parents=True,exist_ok=True);path.write_text('',encoding='utf-8')
            (trial/'grader.py').write_text(case['tests'],encoding='utf-8',newline='\n')
            init_project(workspace,kit,name=f'Evaluation {key}')
            config=workspace/'.orchestra/project.toml'
            config.write_text(config.read_text().replace('max_repair_cycles = 2',f'max_repair_cycles = {repair_cycles}'),encoding='utf-8',newline='\n')
            sync_project(workspace,kit)
            manifest={'schema':SCHEMA,'case':key,'repeat':repeat,'variant':variant,
                      'fingerprint':_case_fingerprint(case),'repair_cycles':repair_cycles,
                      'integration':_integration(workspace)}
            _write_new(trial/'trial.json',manifest)
            trials.append(trial.name)
    manifest={'schema':SCHEMA,'variant':variant,'trials':trials,'repair_cycles':repair_cycles}
    _write_new(output/'suite.json',manifest)
    return {**manifest,'directory':str(output),'model_calls':0}


def _trial(trial: Path):
    if trial.is_symlink():raise ValueError('symlinked trial')
    trial=trial.resolve();manifest=_read(_child(trial,'trial.json'))
    if not isinstance(manifest,dict) or manifest.get('schema')!=SCHEMA or manifest.get('case') not in _cases():
        raise ValueError('unknown evaluation fixture')
    case=_cases()[manifest['case']]
    if manifest.get('fingerprint')!=_case_fingerprint(case):raise ValueError('fixture or environment changed; prepare a fresh run')
    grader=_child(trial,'grader.py')
    if not grader.is_file() or grader.read_text(encoding='utf-8')!=case['tests']:
        raise ValueError('evaluation grader changed')
    if _child(trial,'grader-cache').exists():raise ValueError('external grader cache must remain absent')
    workspace=_child(trial,'workspace')
    if not workspace.is_dir():raise ValueError('missing workspace')
    if manifest.get('integration')!=_integration(workspace):raise ValueError('prepared evaluation instructions or policy changed')
    for name,text in case['files'].items():
        if name not in case['write_paths']:
            path=_child(workspace,name)
            if not path.is_file() or path.read_text(encoding='utf-8')!=text:
                raise ValueError('fixed fixture source changed')
    return manifest,case,workspace


def _subjects(case,workspace):
    names=set(case['files'])|set(case['write_paths'])|{'.orchestra/project.toml','AGENTS.md'}
    return _hash({name:hashlib.sha256(_child(workspace,name).read_bytes()).hexdigest() for name in sorted(names)})


def _scope(case,workspace):
    allowed=set(case['files'])|set(case['write_paths'])|{'AGENTS.md'}
    for parent,dirs,files in os.walk(workspace,followlinks=False):
        for name in [*dirs,*files]:
            if (Path(parent)/name).is_symlink():return False
        dirs[:]=[name for name in dirs if name not in {'.orchestra','.agents','.codex','__pycache__'}]
        for name in files:
            relative=(Path(parent)/name).relative_to(workspace).as_posix()
            if relative not in allowed:return False
    return True


def _checks(trial):
    # Ignore a workspace's stale same-size/mtime .pyc files, including caches
    # produced by a model's own checks. The external cache is never written.
    return [{'argv':[sys.executable,'-I','-B','-X',f'pycache_prefix={trial / "grader-cache"}',str(trial/'grader.py'),str(trial/'workspace')],
             'failure_cause':'implementation'}]


def grade_trial(trial: Path, *, timeout=30) -> dict:
    manifest,case,workspace=_trial(trial)
    if type(timeout) is not int or not 1<=timeout<=120:raise ValueError('invalid grader timeout')
    directory=trial/'grades'/str(time.time_ns());directory.mkdir(parents=True)
    failure,outcomes=run_checks(workspace,_checks(trial),timeout,directory,1)
    _trial(trial)
    return {'case':manifest['case'],'passed':failure is None and _scope(case,workspace),
            'checks':outcomes,'scope_valid':_scope(case,workspace)}


def _job(trial,case,role):
    subjects=sorted(set(case['files'])|set(case['write_paths']))
    scope=', '.join(case['write_paths'])
    brief={'GOAL':case['goal'],'SOURCES OF TRUTH':list(case['files']),
           'SCOPE':f'Only edit {scope}. Preserve all other files. Do not read parent directories, grader, reference solutions or evaluation implementation. Do not delegate, commit or install dependencies.',
           'ACCEPTANCE CRITERIA':case['acceptance'],
           'VERIFICATION':'Use inline Python stdlib checks for the specified behavior; do not create extra test files outside scope. The host runs a separate immutable acceptance grader.',
           'CONTEXT':'Synthetic evaluation project; no network needed. Give a concise report of actual changes and checks.'}
    if role=='reviewer':
        brief['SOURCES OF TRUTH']=subjects
        brief['GOAL']='Independently review the implementation against this original task: '+case['goal']
        brief['SCOPE']='Read only the listed source files and generated outputs. Never edit any file, delegate or read parent directories.'
        brief['CONTEXT']='Return first line VERDICT: PASS or VERDICT: FAIL, then concrete blocking findings. Passing tests alone does not prove full acceptance.'
    return {'role':role,'complexity':'simple','risk':'low','size':'substantial','independent':True,
            'brief':brief,'checks':_checks(trial),'evidence_files':subjects,
            'review_files':subjects if role=='reviewer' else []}


def _suite(root: Path):
    if root.is_symlink():raise ValueError('symlinked suite')
    root=root.resolve();manifest=_read(_child(root,'suite.json'))
    if (not isinstance(manifest,dict) or manifest.get('schema')!=SCHEMA or not isinstance(manifest.get('trials'),list)
            or not manifest['trials'] or len(manifest['trials'])>100
            or len(set(manifest['trials']))!=len(manifest['trials'])):raise ValueError('invalid evaluation suite')
    return root,manifest


def run_suite(root: Path, kit: Path, *, max_calls: int, timeout=180, runner=None) -> dict:
    if type(max_calls) is not int or not 1<=max_calls<=1000:raise ValueError('max_calls must be between 1 and 1000')
    if type(timeout) is not int or not 1<=timeout<=3600:raise ValueError('invalid timeout')
    root,suite=_suite(root);execute=runner or run_job;used=0
    # A suite-level lock prevents a second dashboard/chat/process consuming the
    # same call budget. A stale lock never authorizes automatic replay.
    lock=_child(root,'running.lock')
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError as exc:raise ValueError('evaluation is already running or needs explicit recovery') from exc
    try:
        os.close(fd)
        for name in suite['trials']:
            prior=_child(root,name)/'result.json'
            if prior.exists():
                result=_read(prior)
                used+=sum(len(p['receipt'].get('attempts',[])) for p in result['receipts'])
                if result.get('exception'):
                    # An exception can hide a launched call; it requires manual
                    # reconciliation and never grants another automatic budget.
                    return report_suite(root)
        for name in suite['trials']:
            trial=_child(root,name);manifest,case,workspace=_trial(trial)
            if (trial/'result.json').exists():continue
            if (trial/'started.json').exists():break
            config=load_config(workspace,kit)
            if config.workflow.max_repair_cycles!=manifest['repair_cycles']:raise ValueError('evaluation retry policy changed')
            reservation=2*(config.workflow.max_repair_cycles+1)
            if used+reservation>max_calls:break
            _write_new(trial/'started.json',{'reserved_calls':reservation,'coverage':_COVERAGE})
            started=time.monotonic();receipts=[]
            try:
                def invariant():
                    _trial(trial)
                    return _scope(case,workspace)
                workflow=run_workflow(workspace,kit,_job(trial,case,'worker'),
                                      max_calls=reservation,timeout_seconds=timeout,
                                      runner=execute,acceptance=invariant)
                receipts=workflow['receipts']
                counts={}
                for phase in receipts:
                    role=phase['role'];counts[role]=counts.get(role,0)+1
                    suffix='' if counts[role]==1 else '-'+str(counts[role])
                    _write_new(trial/f'{role}{suffix}-receipt.json',phase['receipt'])
                final_grade=grade_trial(trial)
                result={'schema':SCHEMA,'receipts':receipts,'grade':final_grade,
                        'workflow_status':workflow['status'],'stop_reason':workflow['stop_reason'],
                        'elapsed_seconds':time.monotonic()-started,
                        'exception':'DispatchUncertain' if workflow['model_attempts'] is None else None,
                        'subjects':_subjects(case,workspace)}
            except Exception as exc:
                # Never translate an interrupted/failed launch into zero cost.
                result={'schema':SCHEMA,'receipts':receipts,'grade':{'passed':False},
                        'elapsed_seconds':time.monotonic()-started,'exception':type(exc).__name__}
            _write_new(trial/'result.json',result)
            attempts=sum(len(item['receipt'].get('attempts',[])) for item in receipts)
            used+=reservation if result['exception'] else attempts
            if result['exception'] or used>max_calls:break
    finally:
        lock.unlink()
    return report_suite(root)


def report_suite(root: Path) -> dict:
    root,suite=_suite(root);trials=[];samples=[];observations=[];attempt_count=0;attempts_unknown=False
    for name in suite['trials']:
        trial=_child(root,name);manifest,case,workspace=_trial(trial)
        path=_child(trial,'result.json')
        if not path.exists():
            attempts_unknown=attempts_unknown or (trial/'started.json').exists()
            trials.append({'case':manifest['case'],'repeat':manifest['repeat'],'completed':False,'passed':False});continue
        result=_read(path);phases=result['receipts'];all_attempts=[];complete=not result.get('exception');phase_info=[]
        attempts_unknown=attempts_unknown or bool(result.get('exception'))
        for item in phases:
            receipt=item['receipt'];attempts=receipt.get('attempts',[]);phase_totals=[]
            if not attempts:complete=False
            for attempt in attempts:
                valid,counters=_usage(attempt.get('usage'),'evaluation attempt');complete=complete and valid
                phase_totals.append(counters)
                all_attempts.append({**attempt,'number':len(all_attempts)+1})
            summed=_sum_known(phase_totals) if phase_totals else None
            if summed:observations.extend(phase_totals)
            phase_info.append({'role':item['role'],'attempts':len(attempts),'tokens':summed['total_tokens'] if summed else None})
        attempt_count+=len(all_attempts)
        if not phases:complete=False
        latest_grade=grade_trial(trial)
        workers=[p['receipt'] for p in phases if p['role']=='worker']
        worker=next(iter(reversed(workers)),{})
        reviewer=next((p['receipt'] for p in reversed(phases) if p['role']=='reviewer'),{})
        initial_worker=next(iter((workers[0] if workers else {}).get('attempts',[])),{})
        passed=(not result.get('exception') and result.get('subjects')==_subjects(case,workspace)
                and latest_grade['passed'] and result['grade']['passed']
                and result.get('workflow_status','verified')=='verified'
                and worker.get('status')=='verified' and reviewer.get('status')=='verified'
                and str(reviewer.get('final','')).splitlines()[:1]==['VERDICT: PASS'])
        trials.append({'case':manifest['case'],'repeat':manifest['repeat'],'completed':True,
                       'passed':passed,'usage_complete':complete,'phases':phase_info,
                       'initial_profile':initial_worker.get('profile'),'exception':result.get('exception'),
                       'stop_reason':result.get('stop_reason'),
                       'repairs':max(0,sum(p['attempts'] for p in phase_info if p['role']=='worker')-1)})
        if all_attempts:
            totals=_sum_known([_usage(a.get('usage'),'evaluation attempt')[1] for a in all_attempts])
            samples.append({'variant':manifest['variant'],'sample':{'fixture_fingerprint':manifest['fingerprint'],
                'receipt':{**worker,'profile':initial_worker.get('profile'),
                           'requested_model':initial_worker.get('requested_model'),
                           'requested_effort':initial_worker.get('requested_effort'),
                           'status':'verified' if passed else 'failed','attempts':all_attempts,
                           'usage':{'complete':complete,'totals':totals}},
                'quality':{'checks_passed':latest_grade['passed'],'acceptance_passed':passed,
                           'review_verdict':'PASS' if passed else 'FAIL'},'elapsed_seconds':result['elapsed_seconds']}})
    completed=sum(t['completed'] for t in trials)
    complete=completed==len(trials) and all(t.get('usage_complete',False) for t in trials)
    totals=_sum_known(observations) if complete and observations else None
    accepted=sum(t['passed'] for t in trials)
    accepted_cost=sum(sum(p['tokens'] or 0 for p in t.get('phases',[])) for t in trials if t['passed']) if complete else None
    unresolved_cost=sum(sum(p['tokens'] or 0 for p in t.get('phases',[])) for t in trials if not t['passed']) if complete else None
    return {'schema':SCHEMA,'variant':suite['variant'],'complete':complete,'trial_count':len(trials),
            'completed_trials':completed,'quality_pass_count':accepted,
            'accepted_trials':accepted,'unresolved_trials':len(trials)-accepted,
            'accepted_workflow_tokens':accepted_cost,'unresolved_workflow_tokens':unresolved_cost,
            'observed_tokens_per_accepted_trial':totals['total_tokens']/accepted if totals and accepted else None,
            'model_attempts':None if attempts_unknown else attempt_count,'known_model_attempts':attempt_count,
            'observed_total_tokens':totals['total_tokens'] if totals else None,'totals':totals,
            'coverage':_COVERAGE,'savings_declared':False,'trials':trials,'samples':samples}


def compare_suites(before: Path, after: Path) -> dict:
    reports=[report_suite(before),report_suite(after)]
    if any(not r['complete'] for r in reports):raise ValueError('both suites require complete trials and usage')
    if reports[0]['variant']==reports[1]['variant']:raise ValueError('variants must differ')
    # A cheaper reviewer is a changed quality policy, not a context optimization.
    reviewer_settings=[]
    for root in (before,after):
        root,suite=_suite(root);settings={}
        for name in suite['trials']:
            manifest,case,workspace=_trial(_child(root,name))
            config=load_config(workspace,_kit_for_comparison())
            from .routing import route_task
            route=route_task(config,role='reviewer',complexity='simple',risk='low',size='substantial',independent=True)
            settings[(manifest['case'],manifest['repeat'])]=(route['model'],route['effort'],route['provider'])
        reviewer_settings.append(settings)
    if reviewer_settings[0]!=reviewer_settings[1]:raise ValueError('reviewer settings must match between suites')
    samples=[s for report in reports for s in report['samples']]
    profiles=list(dict.fromkeys(a['profile'] for s in samples for a in s['sample']['receipt']['attempts']))
    # calibrate groups by initial worker profile; subsequent reviewer profiles
    # still need to be declared but do not form independent task samples.
    from .calibration import _sample
    for sample in samples:_sample(sample['sample'],set(profiles))
    initial=list(dict.fromkeys(s['sample']['receipt']['attempts'][0]['profile'] for s in samples))
    # Keep all phase profile labels valid without pretending each reviewer call
    # is a separate completed user task.
    normalized=json.loads(json.dumps(samples))
    for wrapper in normalized:
        sample=wrapper['sample'];first=sample['receipt']['attempts'][0]['profile']
        for attempt in sample['receipt']['attempts']:attempt['profile']=first
    result=compare_workflows(normalized,initial,[r['variant'] for r in reports],baseline=reports[0]['variant'])
    # Mandatory review is a separate phase, never a failed worker retry.
    for report in reports:
        def retries_for(trial):
            return (max(0,sum(p['attempts'] for p in trial['phases'] if p['role']=='worker')-1)
                    +sum(max(0,p['attempts']-1) for p in trial['phases'] if p['role']=='reviewer'))
        label=report['variant'];retries=sum(retries_for(t) for t in report['trials'])
        for profile,metrics in result['variants'][label]['profiles'].items():
            metrics['retries']=sum(retries_for(t) for t in report['trials'] if t['initial_profile']==profile)
        result['variants'][label]['phase_breakdown']={'available':True,'trials':report['trials']}
        for comparison in result['comparisons'].values():
            if label==comparison['baseline']:comparison['baseline_metrics']['retries']=retries
            if label==comparison['candidate']:comparison['candidate_metrics']['retries']=retries
    return result


def _kit_for_comparison():
    packaged=Path(__file__).resolve().parent/'_kit_data'
    return packaged if (packaged/'templates/project.toml').is_file() else Path(__file__).resolve().parents[2]
