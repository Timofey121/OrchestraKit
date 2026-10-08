"""Private append-only phase journal. Events are evidence, never instructions."""
import json
import os
from datetime import datetime,timezone
from pathlib import Path

def append_event(path:Path,*,task_id,leaf_id,attempt,phase,owner,evidence_ref=None,usage=None):
    event={'schema':1,'task_id':task_id,'parent_id':task_id,'leaf_id':leaf_id,
           'attempt':attempt,'phase':phase,'timestamp':datetime.now(timezone.utc).isoformat(),
           'owner':owner,'evidence_ref':evidence_ref,'usage':usage,
           'usage_coverage':'complete' if isinstance(usage,dict) and usage.get('complete') is True else 'unknown'}
    content=(json.dumps(event,ensure_ascii=False,allow_nan=False)+'\n').encode()
    if len(content)>32000:raise ValueError('event exceeds journal bound')
    fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        if os.write(fd,content)!=len(content):raise OSError('incomplete journal write')
        os.fsync(fd)
    finally:os.close(fd)
