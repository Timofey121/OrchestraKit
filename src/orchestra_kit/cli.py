from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import ConfigError, load_config
from .install import check_skill, check_user_setup, install_skill, install_user_setup
from .project import ProjectError, doctor_project, init_project, sync_project
from .routing import COMPLEXITIES, FAILURES, RISKS, build_brief, route_task
from .state import list_tasks, record_task, show_task, start_task


def _kit_root() -> Path:
    packaged = Path(__file__).resolve().parent / "_kit_data"
    if (packaged / "templates/project.toml").is_file():
        return packaged
    return Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestra",
        description="Compile project model profiles into Codex orchestration files.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Initialize and synchronize a project")
    init.add_argument("project", nargs="?", default=".", help="Project directory (default: current directory)")
    init.add_argument("--name", help="Human-readable project name (default: directory name)")

    sync = commands.add_parser("sync", help="Regenerate managed project files")
    sync.add_argument("project", nargs="?", default=".", help="Project directory (default: current directory)")

    doctor = commands.add_parser("doctor", help="Validate configuration, credentials, and generated files")
    doctor.add_argument("project", nargs="?", default=".", help="Project directory (default: current directory)")

    ui = commands.add_parser('ui', help='Open the live local project and chat dashboard')
    ui.add_argument('projects', nargs='*', default=[])
    ui.add_argument('--host', default='127.0.0.1', help='Loopback interface only')
    ui.add_argument('--port', type=int, default=8731)
    ui.add_argument('--codex-home', default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    ui.add_argument('--no-codex', action='store_true', help='Read only explicit Orchestra projects')
    ui.add_argument('--no-browser', action='store_true')
    ui.add_argument('--all-projects', action='store_true', help='Use Codex project discovery without adding the working directory')

    autostart = commands.add_parser('ui-autostart', help='Manage the user dashboard service (macOS, Windows, systemd Linux)')
    autostart.add_argument('action', choices=('install', 'status', 'uninstall'))
    autostart.add_argument('--skills-dir', default=str(Path.home() / '.agents/skills'))
    autostart.add_argument('--codex-home', default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    autostart.add_argument('--port', type=int, default=8731)

    route = commands.add_parser("route", help="Recommend a configured model, effort, and execution policy")
    route.add_argument("project", nargs="?", default=".")
    route.add_argument("--role", required=True)
    route.add_argument("--complexity", choices=COMPLEXITIES, default="standard")
    route.add_argument("--risk", choices=RISKS, default="medium")
    route.add_argument("--size", choices=("small", "substantial"), default="substantial")
    route.add_argument("--independent", action="store_true")
    route.add_argument("--require-capability", action="append", default=[])
    route.add_argument("--failure", choices=FAILURES)
    route.add_argument("--previous-profile")
    route.add_argument("--attempt", type=int, default=0)
    brief = commands.add_parser("brief", help="Validate and render a bounded leaf brief")
    brief.add_argument("project", nargs="?", default=".")
    brief.add_argument("--input", required=True, help="JSON file with the six brief sections")
    recipe = commands.add_parser('recipe', help='Compile a bugfix, feature or refactor task locally; never launches a model')
    recipe.add_argument('project', nargs='?', default='.')
    recipe_mode = recipe.add_mutually_exclusive_group(required=True)
    recipe_mode.add_argument('--input', help='JSON task with recipe, goal, files, scope, acceptance and checks')
    recipe_mode.add_argument('--describe', choices=('bugfix', 'feature', 'refactor'), help='Show only this recipe; no project setup needed')
    task = commands.add_parser("task", help="Store durable progress and verification evidence")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    for name in ("start", "record", "show", "list"):
        child = task_commands.add_parser(name)
        child.add_argument("project", nargs="?", default=".")
        if name in ("record", "show"):
            child.add_argument("task_id")
        if name == "start":
            child.add_argument("--goal", required=True)
            child.add_argument('--chat-id', help='Explicit Codex chat UUID; otherwise CODEX_THREAD_ID when present')
        if name == "record":
            child.add_argument("--input", required=True)
            child.add_argument("--revision", type=int, required=True)
        if name == "list":
            child.add_argument("--limit", type=int, default=20)
    setup = commands.add_parser("setup", help="Install or check the complete local user integration")
    setup.add_argument("--skills-dir", default=str(Path.home() / ".agents/skills"))
    setup.add_argument("--codex-home", default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    setup.add_argument("--check", action="store_true")
    install = commands.add_parser("install-skill", help="Install or check the portable user skill")
    install.add_argument("--skills-dir", default=str(Path.home() / ".agents/skills"))
    install.add_argument("--check", action="store_true")
    install.add_argument("--bootstrap", action="store_true", help="Add managed primary-workflow guidance to Codex global instructions")
    install.add_argument("--codex-home", default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')),
                         help="Codex home for --bootstrap (default: CODEX_HOME or ~/.codex)")
    usage = commands.add_parser('usage', help='Summarize observed session counters, grouped by configured model')
    hooks = commands.add_parser('install-hooks', help='Prepare native hooks; host trust is reviewed separately through /hooks')
    hooks.add_argument('--skills-dir', default=str(Path.home() / '.agents/skills'))
    hooks.add_argument('--codex-home', default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    hooks.add_argument('--check', action='store_true')
    hook = commands.add_parser('hook', help='Handle one native lifecycle JSON event from stdin')
    hook.add_argument('--skill-path', required=True)
    hook.add_argument('--expected-sha256', required=True)
    usage.add_argument('logs', nargs='+', help='Codex session JSONL files, including leaf logs when available')
    activation = commands.add_parser('activation', help='Audit actual skill-reading evidence in public tool logs')
    activation.add_argument('logs', nargs='+')
    activation.add_argument('--skill-path', default=str(Path.home() / '.agents/skills/orchestra/SKILL.md'))
    fingerprint = commands.add_parser('fingerprint', help='Pin or recheck the exact files covered by a review')
    fingerprint.add_argument('project', nargs='?', default='.')
    fingerprint_mode = fingerprint.add_mutually_exclusive_group(required=True)
    fingerprint_mode.add_argument('--path', action='append', dest='paths')
    fingerprint_mode.add_argument('--check', metavar='SNAPSHOT.json')
    execute = commands.add_parser('execute', help='Run one bounded leaf with configured routing and actual checks')
    execute.add_argument('project', nargs='?', default='.')
    execute.add_argument('--input', required=True, help='JSON job containing a bounded brief and check argv')
    execute.add_argument('--codex', help='Codex executable path (default: codex on PATH)')
    execute.add_argument('--timeout', type=int, default=180, help='Seconds per subprocess (default: 180)')
    execute.add_argument('--dry-run', action='store_true')
    execute.add_argument('--workspace',help='Prepared isolated worktree; parent policy stays authoritative')
    execute.add_argument('--write-path',action='append',default=None)
    workflow = commands.add_parser('workflow', help='Run worker, checks, independent review and bounded repairs')
    workflow.add_argument('project', nargs='?', default='.')
    workflow.add_argument('--input', required=True, help='Original worker job JSON, as for execute')
    workflow.add_argument('--max-launches', type=int, required=True)
    workflow.add_argument('--timeout', type=int, default=180)
    workflow.add_argument('--codex')
    workflow.add_argument('--dry-run', action='store_true')
    workflow.add_argument('--reuse', action='store_true', help='Opt in to exact-context verification reuse')
    workflow.add_argument('--environment', help='JSON file of explicit relevant environment conditions; no credentials')
    workflow.add_argument('--dependencies', help='JSON file listing relevant dependency/check/lock files; [] if none')
    consultation=commands.add_parser('consult',help='Ask one strong read-only advisor before root decides on escalation')
    consultation.add_argument('project');consultation.add_argument('--input',required=True)
    consultation.add_argument('--codex');consultation.add_argument('--timeout',type=int,default=180)
    consultation.add_argument('--dry-run',action='store_true')
    question=commands.add_parser('question',help='Record a bounded worker question and root-mediated advisory answer')
    question_actions=question.add_subparsers(dest='question_command',required=True)
    for action in ('ask','answer','read'):
        child=question_actions.add_parser(action);child.add_argument('project')
        if action!='ask':child.add_argument('question_id')
        if action=='read':child.add_argument('--environment',required=True)
        else:child.add_argument('--input',required=True)
    cancel=commands.add_parser('cancel',help='Request cancellation of one active native execution')
    cancel.add_argument('project');cancel.add_argument('execution_id');cancel.add_argument('--reason',required=True)
    evidence=commands.add_parser('evidence',help='Run checks and validate exact source-bound evidence')
    actions=evidence.add_subparsers(dest='evidence_command',required=True)
    run=actions.add_parser('run');run.add_argument('project');run.add_argument('--input',required=True);run.add_argument('--timeout',type=int,default=180)
    check=actions.add_parser('check');check.add_argument('project');check.add_argument('--ref',required=True);check.add_argument('--kind',choices=('check','review'),default='check');check.add_argument('--path',action='append',default=[])
    bind=actions.add_parser('bind',help='Explicit root adoption of current evidence into an existing task')
    bind.add_argument('project');bind.add_argument('--ref',required=True);bind.add_argument('--task-id',required=True)
    bind.add_argument('--kind',choices=('check','review'),required=True)
    handoff=commands.add_parser('handoff',help='Build a bounded continuation tied to a durable task revision')
    handoff.add_argument('project');handoff.add_argument('--input',required=True)
    memory=commands.add_parser('memory',help='Store and find accepted, source-bound advisory solutions')
    memory_actions=memory.add_subparsers(dest='memory_command',required=True)
    memory_store=memory_actions.add_parser('store')
    memory_store.add_argument('project');memory_store.add_argument('--input',required=True)
    memory_search=memory_actions.add_parser('search')
    memory_search.add_argument('project');memory_search.add_argument('--query',required=True)
    memory_search.add_argument('--environment',required=True,help='JSON object of current environment conditions')
    memory_search.add_argument('--limit',type=int,default=3)
    memory_search.add_argument('--max-chars',type=int,default=6000)
    navigation=commands.add_parser('navigate',help='Build or query an optional local source-symbol index')
    navigation_actions=navigation.add_subparsers(dest='navigation_command',required=True)
    navigation_build=navigation_actions.add_parser('build')
    navigation_build.add_argument('project');navigation_build.add_argument('--path',action='append',required=True)
    navigation_build.add_argument('--json',action='store_true',help='Return the full index instead of its summary')
    navigation_search=navigation_actions.add_parser('search')
    navigation_search.add_argument('project');navigation_search.add_argument('--query',required=True)
    navigation_search.add_argument('--limit',type=int,default=10)
    navigation_search.add_argument('--max-chars',type=int,default=6000)
    calibrate=commands.add_parser('calibrate',help='Compare observed receipts for identical fixtures and quality gates')
    calibrate.add_argument('project');calibrate.add_argument('--input',required=True);calibrate.add_argument('--profile',action='append',required=True)
    compare=commands.add_parser('compare-workflows',help='Compare workflow variants on identical fixtures without changing models')
    compare.add_argument('project');compare.add_argument('--input',required=True)
    compare.add_argument('--profile',action='append',required=True)
    compare.add_argument('--variant',action='append',required=True)
    compare.add_argument('--baseline',required=True)
    evaluation=commands.add_parser('eval',help='Prepare and measure reproducible coding tasks; only run invokes models')
    evaluation.set_defaults(project='.')
    evaluations=evaluation.add_subparsers(dest='eval_command',required=True)
    evaluations.add_parser('list')
    prepare=evaluations.add_parser('prepare');prepare.add_argument('--output',required=True)
    prepare.add_argument('--case',action='append');prepare.add_argument('--repeat',type=int,default=2)
    prepare.add_argument('--variant',default='baseline');prepare.add_argument('--repair-cycles',type=int,default=0)
    run_eval=evaluations.add_parser('run');run_eval.add_argument('directory')
    run_eval.add_argument('--max-launches','--max-calls',dest='max_calls',type=int,required=True,
                          help='Limit native Codex CLI launches, not host-internal inference requests')
    run_eval.add_argument('--timeout',type=int,default=180)
    report_eval=evaluations.add_parser('report');report_eval.add_argument('directory')
    grade=evaluations.add_parser('grade');grade.add_argument('directory')
    comparison=evaluations.add_parser('compare');comparison.add_argument('before');comparison.add_argument('after')
    logs=commands.add_parser('log',help='Read or search bounded, redacted retained logs')
    log_actions=logs.add_subparsers(dest='log_command',required=True)
    for action in ('read','search'):
        log_action=log_actions.add_parser(action)
        log_action.add_argument('project');log_action.add_argument('path')
        if action=='search':log_action.add_argument('--query',required=True)
        log_action.add_argument('--start-line',type=int,default=1)
        log_action.add_argument('--max-lines',type=int,default=40)
        log_action.add_argument('--context-lines',type=int,default=2)
        log_action.add_argument('--max-chars',type=int,default=6000)
        log_action.add_argument('--json',action='store_true')
    worktree=commands.add_parser('worktree',help='Create retained task isolation and inspect declared write scope')
    worktree_actions=worktree.add_subparsers(dest='worktree_command',required=True)
    create=worktree_actions.add_parser('create');create.add_argument('project');create.add_argument('--ref',required=True);create.add_argument('--destination',required=True)
    scope=worktree_actions.add_parser('scope');scope.add_argument('project');scope.add_argument('--path',action='append',required=True)
    queue=commands.add_parser('queue',help='Deterministic durable dependency scheduling with fenced leases')
    queue_actions=queue.add_subparsers(dest='queue_command',required=True)
    submit=queue_actions.add_parser('submit');submit.add_argument('project');submit.add_argument('--input',required=True)
    for name in ('claim','inspect','heartbeat','settle','cancel','recover'):
        action=queue_actions.add_parser(name);action.add_argument('project');action.add_argument('graph_id')
        if name in ('heartbeat','settle','cancel','recover'):action.add_argument('node_id')
        if name in ('claim','heartbeat','settle'):action.add_argument('--owner',required=True)
        if name in ('heartbeat','settle'):action.add_argument('--lease-token',required=True)
        if name in ('claim','heartbeat'):action.add_argument('--lease-seconds',type=int,default=60)
        if name in ('settle','recover'):action.add_argument('--input',required=True)
    return parser


def _json_input(path: str) -> object:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    kit = _kit_root()
    try:
        if args.command == 'ui-autostart':
            from .ui_service import install_service, service_status, uninstall_service
            if args.action == 'install':
                result = install_service(Path(args.skills_dir) / 'orchestra', Path(args.codex_home), port=args.port)
            elif args.action == 'uninstall':
                result = uninstall_service()
            else:
                result = service_status()
            _print_json(result)
            return 0
        if args.command == 'recipe':
            from .recipes import compile_recipe, recipe_instructions
            if args.describe:
                _print_json({'recipe': args.describe, **{role: recipe_instructions(args.describe, role) for role in ('worker', 'reviewer')}})
            else:
                _print_json(compile_recipe(Path(args.project).expanduser().resolve(), kit, _json_input(args.input)))
            return 0
        if args.command == 'setup':
            skills_dir = Path(args.skills_dir).expanduser()
            codex_home = Path(args.codex_home).expanduser()
            if args.check:
                errors = check_user_setup(kit, skills_dir, codex_home)
                configuration = 'drifted' if errors else 'current'
            else:
                skill = install_user_setup(kit, skills_dir, codex_home)
                errors = ()
                configuration = 'installed'
            skill = skills_dir.resolve() / 'orchestra'
            _print_json({
                'configuration': configuration,
                'problems': list(errors),
                'components': {
                    'skill': str(skill),
                    'bootstrap': str(codex_home.resolve()),
                    'hooks': str(codex_home.resolve() / 'hooks.json'),
                    'dashboard_command': [sys.executable, str(skill / 'scripts/orchestra.py'), 'ui'],
                },
                'runtime_trust': 'unknown',
                'next_step': 'Open /hooks in Codex, inspect each definition, and approve it in the host.',
            })
            return 1 if errors else 0
        if args.command == 'ui':
            from .dashboard import Dashboard, make_server
            import webbrowser
            if args.all_projects and (args.no_codex or args.projects):
                raise ValueError('--all-projects cannot be combined with explicit projects or --no-codex')
            dashboard = Dashboard([] if args.all_projects else [Path(p).expanduser() for p in (args.projects or ['.'])],
                                  None if args.no_codex else Path(args.codex_home).expanduser(), kit)
            server = make_server(dashboard, host=args.host, port=args.port,
                                 static_root=Path(__file__).with_name('web'))
            address = server.server_address
            host = '[' + address[0] + ']' if ':' in address[0] else address[0]
            url = f'http://{host}:{address[1]}'
            print('Orchestra UI: ' + url, flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            try:
                server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        if args.command == 'hook':
            from .hooks import handle_hook
            raw = sys.stdin.read(1_000_001)
            if len(raw.encode('utf-8')) > 1_000_000:
                raise ValueError('hook input exceeds byte budget')
            result = handle_hook(json.loads(raw), Path(args.skill_path), args.expected_sha256,
                                 leaf=os.environ.get('ORCHESTRA_LEAF_EXECUTION') == '1')
            _print_json(result)
            return 0
        if args.command == 'install-hooks':
            from .hooks import check_hooks, install_hooks
            skill = Path(args.skills_dir).expanduser().resolve() / 'orchestra'
            codex_home = Path(args.codex_home).expanduser()
            errors = check_skill(kit, Path(args.skills_dir))
            if errors:
                raise ProjectError('; '.join(errors))
            if args.check:
                errors = check_hooks(codex_home, skill)
                _print_json({'configuration': 'drifted' if errors else 'current',
                             'problems': list(errors), 'runtime_trust': 'unknown',
                             'next_step': 'Inspect hooks/list or review /hooks in Codex.'})
                return 1 if errors else 0
            install_hooks(codex_home, skill)
            _print_json({'configuration': 'installed', 'runtime_trust': 'unknown',
                         'next_step': 'Review the exact new hook definitions through /hooks.'})
            return 0
        if args.command == 'usage':
            from .usage import summarize_usage
            _print_json(summarize_usage(Path(p) for p in args.logs))
            return 0
        if args.command == 'activation':
            from .activation import audit_activation
            result = audit_activation((Path(p).expanduser() for p in args.logs),
                                      Path(args.skill_path).expanduser())
            _print_json(result)
            return 0 if result['status'] == 'pass' else 1
        if args.command == "install-skill":
            destination = Path(args.skills_dir).expanduser()
            codex_home = Path(args.codex_home).expanduser() if args.bootstrap else None
            if args.check:
                errors = check_skill(kit, destination, codex_home=codex_home)
                for error in errors:
                    print(f"error: {error}", file=sys.stderr)
                if not errors:
                    print(f"healthy skill: {destination / 'orchestra'}")
                return 1 if errors else 0
            print(f"installed skill: {install_skill(kit, destination, codex_home=codex_home)}")
            if codex_home is not None:
                print(f"installed workflow bootstrap in: {codex_home}")
            return 0
        project = Path(args.project).expanduser()
        if args.command=='cancel':
            from .execution import cancel_execution
            _print_json(cancel_execution(project,args.execution_id,args.reason));return 0
        if args.command=='evidence':
            from .evidence import run_evidence,verify_evidence,bind_evidence
            if args.evidence_command=='bind':
                _print_json(bind_evidence(project,args.task_id,args.ref,kind=args.kind));return 0
            if args.evidence_command=='run':
                data=_json_input(args.input)
                if not isinstance(data,dict) or set(data)!={'files','checks'}:raise ValueError('evidence input requires files and checks')
                result=run_evidence(project,data['files'],data['checks'],timeout_seconds=args.timeout)
                _print_json(result);return 0 if result['status']=='verified' else 1
            result=verify_evidence(project,args.ref,kind=args.kind,required_files=args.path)
            _print_json({'status':'current','snapshot':result['snapshot']});return 0
        if args.command=='handoff':
            from .handoff import build_handoff,validate_handoff_evidence
            result=build_handoff(_json_input(args.input),max_chars=load_config(project,kit).context.max_result_chars)
            state=show_task(project,result['task_id'])
            if state['revision']!=result['revision'] or state['goal']!=result['goal']:raise ValueError('handoff identity is stale or mismatched')
            validate_handoff_evidence(project,result)
            from .evidence import _safe
            from .state import _write
            path=_safe(project,f".orchestra/handoffs/{result['task_id']}-{result['revision']}.json")
            if path.exists():
                if json.loads(path.read_text())!=result:raise ValueError('handoff revision already has different content')
            else:_write(path,result)
            _print_json(result);return 0
        if args.command=='memory':
            from .memory import store_solution,search_solutions
            load_config(project,kit)
            if args.memory_command=='store':
                result=store_solution(project,kit,_json_input(args.input))
            else:
                result=search_solutions(project,args.query,environment=_json_input(args.environment),
                                        limit=args.limit,max_chars=args.max_chars)
                # Match the API's compact budget; avoid indentation/newline overhead.
                sys.stdout.write(json.dumps(result,ensure_ascii=False,separators=(',',':')));return 0
            _print_json(result);return 0
        if args.command=='consult':
            from .consultation import consult
            result=consult(project,kit,_json_input(args.input),
                           codex_command=(args.codex,) if args.codex else None,
                           timeout_seconds=args.timeout,dry_run=args.dry_run)
            _print_json(result)
            return 0 if result['status'] in {'advisory','advisory-dry-run'} else 1
        if args.command=='question':
            from .questions import ask_question,answer_question,read_question
            if args.question_command=='ask':result=ask_question(project,kit,_json_input(args.input))
            elif args.question_command=='answer':result=answer_question(project,kit,args.question_id,_json_input(args.input))
            else:result=read_question(project,kit,args.question_id,environment=_json_input(args.environment))
            sys.stdout.write(json.dumps(result,ensure_ascii=False,separators=(',',':')))
            return 1 if result['status']=='stale' else 0
        if args.command=='navigate':
            from .navigation import build_index,search_index
            if args.navigation_command=='build':
                result=build_index(project,args.path)
                if not args.json:
                    statuses={}
                    for entry in result['files']:statuses[entry['status']]=statuses.get(entry['status'],0)+1
                    result={'index_ref':'.orchestra/navigation/index.json','files':len(result['files']),
                            'symbols':sum(len(entry['symbols']) for entry in result['files']),
                            'statuses':statuses,'advisory':True}
            else:
                result=search_index(project,args.query,limit=args.limit,max_chars=args.max_chars)
                sys.stdout.write(json.dumps(result,ensure_ascii=False,separators=(',',':')));return 0
            _print_json(result);return 0
        if args.command=='eval':
            from .evaluation import list_cases,prepare_suite,run_suite,report_suite,grade_trial,compare_suites
            if args.eval_command=='list':result=list_cases()
            elif args.eval_command=='prepare':
                result=prepare_suite(Path(args.output).expanduser(),kit,case_ids=args.case,repeats=args.repeat,
                                     variant=args.variant,repair_cycles=args.repair_cycles)
            elif args.eval_command=='run':
                result=run_suite(Path(args.directory).expanduser(),kit,max_calls=args.max_calls,timeout=args.timeout)
            elif args.eval_command=='report':result=report_suite(Path(args.directory).expanduser())
            elif args.eval_command=='grade':result=grade_trial(Path(args.directory).expanduser())
            else:result=compare_suites(Path(args.before).expanduser(),Path(args.after).expanduser())
            _print_json(result);return 0
        if args.command=='calibrate':
            from .calibration import calibrate_receipts
            config=load_config(project,kit)
            if set(args.profile)-set(config.profiles):raise ValueError('calibration profiles must be configured')
            _print_json(calibrate_receipts(_json_input(args.input),args.profile));return 0
        if args.command=='compare-workflows':
            from .calibration import compare_workflows
            config=load_config(project,kit)
            if any(profile not in config.profiles for profile in args.profile):
                raise ValueError('unknown profile for workflow comparison')
            _print_json(compare_workflows(_json_input(args.input),args.profile,args.variant,baseline=args.baseline));return 0
        if args.command=='log':
            from .diagnostics import query_log
            result=query_log(project,args.path,query=getattr(args,'query',None),start_line=args.start_line,
                             max_lines=args.max_lines,context_lines=args.context_lines,max_chars=args.max_chars)
            if args.json:_print_json(result)
            else:
                for line in result['lines']:print(f"{result['path']}:{line['line_number']}: {line['text']}")
                print(f"[more={str(result['more']).lower()} next_start_line={result['next_start_line']} incomplete={str(result['incomplete']).lower()}]")
            return 0
        if args.command=='worktree':
            from .isolation import prepare_task_worktree,check_write_scope
            result=prepare_task_worktree(project,kit,args.ref,Path(args.destination).expanduser()) if args.worktree_command=='create' else check_write_scope(project,args.path)
            _print_json(result);return 1 if result.get('status')=='out-of-scope' else 0
        if args.command=='queue':
            from .queue import Queue
            queue=Queue(project,kit)
            if args.queue_command=='submit':
                data=_json_input(args.input)
                if not isinstance(data,dict) or set(data)!={'graph_id','nodes'}:raise ValueError('queue input requires graph_id and nodes')
                queue.submit(data['graph_id'],data['nodes']);result=queue.inspect(data['graph_id'])
            elif args.queue_command=='claim':result=queue.claim(args.graph_id,args.owner,args.lease_seconds)
            elif args.queue_command=='inspect':result=queue.inspect(args.graph_id)
            elif args.queue_command=='heartbeat':result=queue.heartbeat(args.graph_id,args.node_id,args.owner,args.lease_token,args.lease_seconds)
            elif args.queue_command=='cancel':queue.cancel(args.graph_id,args.node_id);result=queue.inspect(args.graph_id)
            elif args.queue_command=='recover':
                data=_json_input(args.input)
                if not isinstance(data,dict) or set(data)!={'execution_absent','descendants_absent','authorized','disposition'}:raise ValueError('invalid explicit recovery evidence')
                queue.recover(args.graph_id,args.node_id,**data);result=queue.inspect(args.graph_id)
            else:
                data=_json_input(args.input)
                if not isinstance(data,dict) or 'status' not in data or not isinstance(data['status'],str) or set(data)-{'status','result','observed_tokens','evidence_ref'}:raise ValueError('invalid queue settlement')
                if data.get('status')=='verified':
                    from .state import _validate_result
                    from .evidence import verify_evidence
                    quality=data.get('result');_validate_result(load_config(project,kit),quality)
                    if quality['status']!='completed':raise ValueError('queue verification requires completed root acceptance')
                    details=next((n for n in queue.inspect(args.graph_id)['tasks'] if n['id']==args.node_id),None)
                    if details is None:raise ValueError('unknown queue node')
                    expected=set(details['acceptance_refs']);actual={a['criterion'] for a in quality.get('acceptance',[])}
                    if expected-actual:raise ValueError('queue acceptance criteria not covered')
                    for kind,records in [('check',quality['checks']),('review',quality['reviews'])]:
                        for record in records:
                            if kind=='check' and not record.get('required',True):continue
                            if not record.get('evidence_ref'):raise ValueError('queue settlement requires fresh evidence refs')
                            subjects=set(quality.get('changed_files',[]))|set((details.get('source_snapshot') or {}).get('files',{}))
                            verify_evidence(project,record['evidence_ref'],kind=kind,required_files=subjects)
                    if data.get('evidence_ref') not in {r['evidence_ref'] for r in quality['checks'] if r.get('required',True)}:raise ValueError('settlement reference must identify required check evidence')
                queue.finish(args.graph_id,args.node_id,args.owner,args.lease_token,data['status'],data.get('evidence_ref'),data.get('observed_tokens'))
                result=queue.inspect(args.graph_id)
            _print_json(result);return 0
        if args.command == 'fingerprint':
            from .fingerprint import fingerprint_paths, check_fingerprint
            if args.check:
                problems = check_fingerprint(project, _json_input(args.check))
                _print_json({'status': 'stale' if problems else 'current', 'problems': list(problems)})
                return 1 if problems else 0
            _print_json(fingerprint_paths(project, args.paths))
            return 0
        if args.command == 'workflow':
            from .workflow import run_workflow
            result=run_workflow(project,kit,_json_input(args.input),max_calls=args.max_launches,
                timeout_seconds=args.timeout,codex_command=[args.codex] if args.codex else None,
                dry_run=args.dry_run,reuse=args.reuse,
                environment=_json_input(args.environment) if args.environment else None,
                dependencies=_json_input(args.dependencies) if args.dependencies else None)
            _print_json(result)
            return 0 if result['status'] in {'verified','dry-run','keep-root'} else 1
        if args.command == 'execute':
            from .execution import run_job
            result = run_job(project, kit, _json_input(args.input),
                codex_command=[args.codex] if args.codex else None,
                timeout_seconds=args.timeout, dry_run=args.dry_run,
                workspace=Path(args.workspace).expanduser() if args.workspace else None,write_files=args.write_path)
            _print_json(result)
            return 0 if args.dry_run or result['status'] in {'verified', 'keep-root'} else 1
        if args.command == "init":
            result = init_project(project, kit, args.name)
            print(f"initialized {project.resolve()}")
            print(f"managed files written: {len(result.written)}")
            return 0
        if args.command == "sync":
            result = sync_project(project, kit)
            print(f"synchronized {project.resolve()}")
            print(f"written: {len(result.written)}; removed: {len(result.removed)}")
            return 0
        if args.command == "doctor":
            result = doctor_project(project, kit)
            for warning in result.warnings:
                print(f"warning: {warning}", file=sys.stderr)
            if not result.ok:
                for error in result.errors:
                    print(f"error: {error}", file=sys.stderr)
                return 1
            print(f"healthy: {project.resolve()}")
            return 0
        if args.command == "route":
            _print_json(route_task(load_config(project, kit), role=args.role,
                complexity=args.complexity, risk=args.risk, size=args.size,
                independent=args.independent, required_capabilities=args.require_capability,
                failure=args.failure, previous_profile=args.previous_profile, attempt=args.attempt))
            return 0
        if args.command == "brief":
            print(build_brief(load_config(project, kit), _json_input(args.input)))
            return 0
        if args.command == "task":
            if args.task_command == "start":
                result = start_task(project, kit, args.goal,
                                    chat_id=args.chat_id or os.environ.get('CODEX_THREAD_ID'))
            elif args.task_command == "record":
                result = record_task(project, kit, args.task_id, _json_input(args.input),
                                     expected_revision=args.revision)
            elif args.task_command == "show":
                result = show_task(project, args.task_id)
            else:
                result = list_tasks(project, limit=args.limit)
            _print_json(result)
            return 0
    except (ConfigError, ProjectError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled command: {args.command}")
