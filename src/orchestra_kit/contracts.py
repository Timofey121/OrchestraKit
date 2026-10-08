"""Small declared JSON result contracts; normalization never changes values."""
import json

TYPES={'object':dict,'array':list,'string':str,'integer':int,'boolean':bool}

def validate_contract(contract):
    if not isinstance(contract,dict) or set(contract)!={'required','aliases'}:
        raise ValueError('result contract requires required and aliases')
    required,aliases=contract['required'],contract['aliases']
    if not isinstance(required,dict) or not required or len(required)>32 or not all(isinstance(k,str) and k and isinstance(v,str) and v in TYPES for k,v in required.items()):
        raise ValueError('invalid result contract required fields')
    if not isinstance(aliases,dict) or len(aliases)>32 or not all(isinstance(k,str) and k and isinstance(v,str) and v in required and k!=v and k not in required for k,v in aliases.items()):
        raise ValueError('invalid result contract aliases')
    if len(set(aliases.values()))!=len(aliases):
        raise ValueError('ambiguous result contract aliases')
    return contract

def normalize_result(text,contract):
    validate_contract(contract)
    def pairs(items):
        out={}
        for k,v in items:
            if k in out:raise ValueError('duplicate result key')
            out[k]=v
        return out
    value=json.loads(text,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite result')))
    if not isinstance(value,dict):raise ValueError('result contract requires an object')
    changes=[]
    for alias,target in contract['aliases'].items():
        if alias in value:
            if target in value:raise ValueError('ambiguous result wrapper')
            value[target]=value.pop(alias);changes.append({'from':alias,'to':target})
    for k,t in contract['required'].items():
        if k not in value or type(value[k]) is not TYPES[t]:raise ValueError(f'result contract field {k} must be {t}')
    return json.dumps(value,ensure_ascii=False,allow_nan=False),changes
