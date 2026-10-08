"""Bind actual checks and independent review reports to exact source files."""
import hashlib
import json
import os
import uuid
from pathlib import Path
from .check_execution import run_checks,validate_checks
from .fingerprint import fingerprint_paths,check_fingerprint

def _safe(project:Path,relative:str)->Path:
    if not isinstance(relative,str) or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('evidence reference must be project-relative')
    root=project.resolve();p=root/relative
    current=root
    for part in Path(relative).parts:
        current=current/part
        if current.is_symlink():raise ValueError('symlinked evidence path')
    if root not in p.resolve().parents:raise ValueError('evidence reference escapes project')
    return p

def _digest(path:Path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for c in iter(lambda:f.read(131072),b''):h.update(c)
    return h.hexdigest()

def create_evidence(project,files,checks,*,review_verdict=None,owner=None,snapshot=None,review_report=None):
    snapshot=snapshot or fingerprint_paths(project,files)
    if check_fingerprint(project,snapshot):raise ValueError('stale evidence subjects')
    if not checks or any(type(c.get('returncode')) is not int or c['returncode']!=0 or not c.get('log') for c in checks):
        raise ValueError('evidence requires actual passing check artifacts')
    artifacts=[]
    for c in checks:
        log=Path(c['log']).resolve()
        if project.resolve() not in log.parents:raise ValueError('check artifact escapes project')
        relative=log.relative_to(project.resolve()).as_posix();_safe(project,relative)
        artifacts.append({'path':relative,'sha256':_digest(log)})
    if review_verdict not in {None,'PASS','FAIL'}:raise ValueError('invalid review verdict')
    report_artifact=None
    if review_verdict is not None:
        if review_report is None:raise ValueError('review evidence requires the actual review report artifact')
        report=Path(review_report)
        relative=report.resolve().relative_to(project.resolve()).as_posix()
        report=_safe(project,relative)
        if report.stat().st_size>1_000_000:raise ValueError('review report too large')
        lines=report.read_text().splitlines()
        if not lines or lines[0].strip()!=f'VERDICT: {review_verdict}':raise ValueError('review report verdict mismatch')
        report_artifact={'path':relative,'sha256':_digest(report)}
    ref=f'.orchestra/evidence/{uuid.uuid4()}.json';path=_safe(project,ref)
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    payload={'schema':1,'owner':owner,'snapshot':snapshot,'checks':checks,'artifacts':artifacts,'review_verdict':review_verdict,'review_report':report_artifact}
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(payload,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
    return ref

def verify_evidence(project,ref,*,kind,required_files=()):
    if not isinstance(ref,str) or not ref.startswith('.orchestra/evidence/') or not ref.endswith('.json'):
        raise ValueError('evidence_ref must identify a generated evidence certificate')
    path=_safe(project,ref)
    if path.stat().st_size>200000:raise ValueError('evidence artifact too large')
    cert=json.loads(path.read_text())
    if not isinstance(cert,dict) or type(cert.get('schema')) is not int or cert['schema']!=1 or set(cert)!={'schema','owner','snapshot','checks','artifacts','review_verdict','review_report'}:raise ValueError('invalid evidence certificate')
    snapshot=cert.get('snapshot');stale=check_fingerprint(project,snapshot)
    if stale:raise ValueError('stale evidence: '+'; '.join(stale))
    if set(required_files)-set(snapshot['files']):raise ValueError('evidence does not cover required subjects')
    if not isinstance(cert['checks'],list) or not cert['checks'] or any(not isinstance(c,dict) or type(c.get('returncode')) is not int or c['returncode']!=0 for c in cert['checks']):raise ValueError('evidence checks did not pass')
    if kind not in {'check','review'}:raise ValueError('invalid evidence kind')
    if kind=='review' and cert.get('review_verdict')!='PASS':raise ValueError('review evidence must have independent PASS')
    if not isinstance(cert['artifacts'],list) or not cert['artifacts'] or len(cert['artifacts'])!=len(cert['checks']) or any(not isinstance(a,dict) or set(a)!={'path','sha256'} for a in cert['artifacts']):raise ValueError('missing or invalid check artifacts')
    for a in cert['artifacts']:
        if _digest(_safe(project,a['path']))!=a['sha256']:raise ValueError('evidence artifact changed')
    if kind=='review':
        report=cert.get('review_report')
        if not isinstance(report,dict) or set(report)!={'path','sha256'} or _digest(_safe(project,report['path']))!=report['sha256']:raise ValueError('review artifact changed or missing')
    return cert

def run_evidence(project,files,checks,*,timeout_seconds=180):
    if type(timeout_seconds) is not int or not 1<=timeout_seconds<=3600:raise ValueError('invalid check timeout')
    checks=validate_checks(checks);snapshot=fingerprint_paths(project,files)
    ref=f'.orchestra/checks/{uuid.uuid4()}';directory=_safe(project,ref);directory.mkdir(parents=True,mode=0o700)
    failure,outcomes=run_checks(project,checks,timeout_seconds,directory,1)
    stale=check_fingerprint(project,snapshot)
    evidence_ref=create_evidence(project,files,outcomes,snapshot=snapshot,owner=directory.name) if failure is None and not stale else None
    return {'status':'verified' if evidence_ref else 'failed','evidence_ref':evidence_ref,'checks':outcomes,'failure':failure or ('; '.join(stale) if stale else None)}
