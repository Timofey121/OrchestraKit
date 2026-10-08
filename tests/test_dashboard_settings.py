from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from orchestra_kit.project import init_project,doctor_project
from orchestra_kit.settings import SettingsService,SettingsConflict
from orchestra_kit.config import load_config
KIT=Path(__file__).resolve().parents[1]
class FakeCredentials:
 def __init__(self): self.values={}
 def status(self,p): return SimpleNamespace(available=(p.base_url,p.env_key) in self.values,source='keychain' if (p.base_url,p.env_key) in self.values else None)
 def set(self,p,key): self.values[p.base_url,p.env_key]=key
 def delete(self,p): self.values.pop((p.base_url,p.env_key),None)
class SettingsTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)/'project';self.root.mkdir();init_project(self.root,KIT)
  self.keys=FakeCredentials();self.service=SettingsService(KIT,credentials=self.keys)
 def payload(self):
  current=self.service.get(self.root)
  return {'expected_fingerprint':current['fingerprint'],'profiles':current['profiles'],'provider':{'id':'gateway','name':'My gateway','base_url':'https://example.test/v1','env_key':'GATEWAY_API_KEY','capabilities':['function','apply_patch']},'api_key':'unit-test-placeholder'}
 def catalog(self):
  return {'models':[{'slug':'custom-small','display_name':'Custom Small','supported_reasoning_levels':[{'effort':'low','description':'Fast'}],'shell_type':'shell_command','visibility':'list','supported_in_api':True,'priority':1,'support_verbosity':False,'truncation_policy':{'mode':'tokens','limit':10000},'experimental_supported_tools':[],'base_instructions':'QA model instructions'}]}
 def test_get_exposes_role_routing_and_configured_model_choices(self):
  result=self.service.get(self.root)
  config=load_config(self.root,KIT)
  self.assertEqual(result['roles'],{name:role.profile for name,role in config.roles.items()})
  self.assertEqual(result['routing'],{'enabled':config.routing.enabled,'profile_order':list(config.routing.profile_order)})
  choice=next(item for item in result['model_options'] if item['id']==config.profiles['cheap'].model)
  self.assertEqual(choice['source'],'configured_profile')
  self.assertIsNone(choice['provider'])
 def test_save_catalog_role_and_routing_round_trip_into_runtime_config(self):
  payload=self.payload();payload['provider']['catalog']=self.catalog();payload['profiles']['cheap']={'model':'custom-small','effort':'low','provider':'gateway'}
  payload['roles']={'worker':'cheap'};payload['routing']={'enabled':False,'profile_order':['critical','strong','balanced','cheap']}
  result=self.service.save(self.root,payload);config=load_config(self.root,KIT)
  self.assertEqual(config.providers['gateway'].model_catalog_json,'.orchestra/providers/gateway-models.json')
  self.assertEqual(config.profiles['cheap'].model,'custom-small');self.assertEqual(config.profiles['cheap'].provider,'gateway')
  self.assertEqual(config.roles['worker'].profile,'cheap');self.assertFalse(config.routing.enabled)
  self.assertEqual(config.routing.profile_order,('critical','strong','balanced','cheap'))
  catalog_path=self.root/config.providers['gateway'].model_catalog_json
  self.assertEqual(json.loads(catalog_path.read_text()),self.catalog())
  self.assertEqual(result['providers']['gateway']['catalog'],self.catalog())
  choice=next(item for item in result['model_options'] if item['id']=='custom-small' and item['provider']=='gateway')
  self.assertEqual(choice,{'id':'custom-small','label':'Custom Small','efforts':['low'],'source':'provider_catalog','provider':'gateway'})
  from orchestra_kit.providers import ProviderCredentialService,resolve_provider_runtime
  runtime=resolve_provider_runtime(config.providers['gateway'],credential_service=ProviderCredentialService(environment={'GATEWAY_API_KEY':'runtime-placeholder'}))
  self.assertIn('model_provider="gateway"',runtime.argv_config);self.assertEqual(runtime.environment,{'GATEWAY_API_KEY':'runtime-placeholder'})
 def test_role_and_routing_validation_rejects_before_mutation(self):
  cases=[('role',{'roles':{'missing':'cheap'}}),('profile',{'roles':{'worker':'missing'}}),('order',{'routing':{'enabled':True,'profile_order':['cheap']}})]
  for name,change in cases:
   with self.subTest(name=name):
    payload=self.payload();payload.update(change);before=(self.root/'.orchestra/project.toml').read_bytes()
    with self.assertRaises(ValueError): self.service.save(self.root,payload)
    self.assertEqual((self.root/'.orchestra/project.toml').read_bytes(),before);self.assertEqual(self.keys.values,{})
 def test_catalog_validation_rejects_bad_or_oversized_models_before_mutation(self):
  invalid=[{'models':[]},{'models':[{'slug':'bad id','display_name':'Bad'}]}, {'models':[{'slug':'same','display_name':'One'},{'slug':'same','display_name':'Two'}]}, {'models':[{'slug':'ok','display_name':'Okay','supported_reasoning_levels':[{'effort':'impossible'}]}]}, {'models':[{'slug':'ok','display_name':'Okay','metadata':{'access_token':'secret'}}]}, {'models':[{'slug':'ok','display_name':'x'*(256*1024)}]}]
  for catalog in invalid:
   with self.subTest(catalog=str(catalog)[:80]):
    payload=self.payload();payload['provider']['catalog']=catalog;before=(self.root/'.orchestra/project.toml').read_bytes()
    with self.assertRaisesRegex(ValueError,'[Кк]аталог|модел'):
     self.service.save(self.root,payload)
    self.assertEqual((self.root/'.orchestra/project.toml').read_bytes(),before);self.assertFalse((self.root/'.orchestra/providers/gateway-models.json').exists())
 def test_invalid_catalog_metadata_is_rejected_without_configuration_changes(self):
  payload=self.payload();payload['provider']['catalog']=self.catalog()
  payload['provider']['catalog']['models'][0]['supported_in_api']='yes'
  before=(self.root/'.orchestra/project.toml').read_bytes()
  with self.assertRaisesRegex(ValueError,'supported_in_api'):
   self.service.save(self.root,payload)
  self.assertEqual((self.root/'.orchestra/project.toml').read_bytes(),before)
  self.assertFalse((self.root/'.orchestra/providers/gateway-models.json').exists())
 def test_catalog_change_participates_in_stale_fingerprint(self):
  payload=self.payload();payload['provider']['catalog']=self.catalog();self.service.save(self.root,payload)
  stale=self.service.get(self.root);path=self.root/'.orchestra/providers/gateway-models.json';path.write_text(json.dumps({'models':[{'slug':'changed','display_name':'Changed'}]}))
  before=(self.root/'.orchestra/project.toml').read_bytes()
  with self.assertRaises(SettingsConflict): self.service.save(self.root,{'expected_fingerprint':stale['fingerprint'],'profiles':stale['profiles']})
  self.assertEqual((self.root/'.orchestra/project.toml').read_bytes(),before)
 def test_existing_catalog_is_preserved_when_omitted_and_replaced_when_explicit(self):
  payload=self.payload();payload['provider']['catalog']=self.catalog();current=self.service.save(self.root,payload)
  provider={'id':'gateway','name':'Renamed gateway','base_url':'https://example.test/v1','env_key':'GATEWAY_API_KEY','capabilities':['function']}
  preserved=self.service.save(self.root,{'expected_fingerprint':current['fingerprint'],'profiles':current['profiles'],'provider':provider})
  path=self.root/'.orchestra/providers/gateway-models.json';self.assertEqual(json.loads(path.read_text()),self.catalog())
  replacement={'models':[{**self.catalog()['models'][0],'slug':'custom-large','display_name':'Custom Large','supported_reasoning_levels':[{'effort':'high','description':'High'}]}]}
  provider['catalog']=replacement
  updated=self.service.save(self.root,{'expected_fingerprint':preserved['fingerprint'],'profiles':preserved['profiles'],'provider':provider})
  self.assertEqual(json.loads(path.read_text()),replacement);self.assertEqual(updated['providers']['gateway']['catalog'],replacement)
 def test_catalog_refuses_symlink_and_unowned_existing_file(self):
  target=self.root/'.orchestra/providers/gateway-models.json';target.parent.mkdir();target.write_text('{}')
  payload=self.payload();payload['provider']['catalog']=self.catalog()
  with self.assertRaisesRegex(ValueError,'существ|принадлеж|каталог'):
   self.service.save(self.root,payload)
  target.unlink();outside=self.root/'outside.json';outside.write_text('{}');target.symlink_to(outside)
  with self.assertRaisesRegex(ValueError,'символ|каталог'):
   self.service.save(self.root,payload)
  self.assertEqual(outside.read_text(),'{}')
 def test_catalog_rolls_back_when_credential_save_fails(self):
  class FailingCredentials(FakeCredentials):
   def set(self,p,key): raise RuntimeError('keychain failed')
  service=SettingsService(KIT,credentials=FailingCredentials());current=service.get(self.root)
  payload={'expected_fingerprint':current['fingerprint'],'profiles':current['profiles'],'provider':{'id':'gateway','name':'My gateway','base_url':'https://example.test/v1','env_key':'GATEWAY_API_KEY','capabilities':[],'catalog':self.catalog()},'api_key':'placeholder'}
  before=(self.root/'.orchestra/project.toml').read_bytes()
  with self.assertRaisesRegex(RuntimeError,'keychain failed'): service.save(self.root,payload)
  self.assertEqual((self.root/'.orchestra/project.toml').read_bytes(),before);self.assertFalse((self.root/'.orchestra/providers/gateway-models.json').exists())
 def test_catalog_rolls_back_when_sync_fails_and_rejects_literal_secret(self):
  from unittest.mock import patch
  payload=self.payload();payload['provider']['catalog']=self.catalog();before=(self.root/'.orchestra/project.toml').read_bytes()
  with patch('orchestra_kit.settings.sync_project',side_effect=RuntimeError('sync failed')):
   with self.assertRaisesRegex(RuntimeError,'sync failed'): self.service.save(self.root,payload)
  self.assertEqual((self.root/'.orchestra/project.toml').read_bytes(),before);self.assertFalse((self.root/'.orchestra/providers/gateway-models.json').exists());self.assertEqual(self.keys.values,{})
  payload=self.payload();payload['provider']['catalog']={'models':[{**self.catalog()['models'][0],'slug':'ok','display_name':'unit-test-placeholder'}]}
  with self.assertRaisesRegex(ValueError,'API-ключ'): self.service.save(self.root,payload)
  self.assertFalse((self.root/'.orchestra/providers/gateway-models.json').exists())
 def test_provider_without_auth_preserves_null_env_and_rejects_secret(self):
  payload=self.payload();payload['provider']['env_key']=None;payload.pop('api_key');result=self.service.save(self.root,payload)
  self.assertIsNone(result['providers']['gateway']['env_key']);self.assertEqual(result['providers']['gateway']['credential'],{'available':True,'source':'not_required'})
  current=self.service.get(self.root);payload={'expected_fingerprint':current['fingerprint'],'profiles':current['profiles'],'provider':{'id':'local','name':'Local','base_url':'http://localhost:11434/v1','env_key':None,'capabilities':[]},'api_key':'must-not-save'}
  with self.assertRaisesRegex(ValueError,'ключ'): self.service.save(self.root,payload)
 def test_save_provider_profiles_syncs_without_persisting_secret(self):
  payload=self.payload();payload['profiles']['cheap']={'model':'custom-small','effort':'low','provider':'gateway'}
  result=self.service.save(self.root,payload)
  self.assertEqual(result['profiles']['cheap']['model'],'custom-small')
  self.assertTrue(result['providers']['gateway']['credential']['available'])
  for path in self.root.rglob('*'):
   if path.is_file(): self.assertNotIn('unit-test-placeholder',path.read_text())
  self.assertFalse(doctor_project(self.root,KIT,environ={'GATEWAY_API_KEY':'placeholder'}).errors)
 def test_stale_edits_reject_without_writing_or_saving_key(self):
  payload=self.payload();p=self.root/'.orchestra/project.toml';p.write_text(p.read_text()+'\n# concurrent change\n');before=p.read_bytes()
  with self.assertRaises(SettingsConflict): self.service.save(self.root,payload)
  self.assertEqual(p.read_bytes(),before);self.assertEqual(self.keys.values,{})
 def test_validation_rejects_unknown_provider_and_remote_http_before_mutation(self):
  for field in ['unknown','http']:
   payload=self.payload()
   if field=='unknown': payload['profiles']['cheap']['provider']='missing'
   else: payload['provider']['base_url']='http://example.test/v1'
   before=(self.root/'.orchestra/project.toml').read_bytes()
   with self.assertRaises(ValueError): self.service.save(self.root,payload)
   self.assertEqual((self.root/'.orchestra/project.toml').read_bytes(),before);self.assertFalse(self.keys.values)
 def test_unmanaged_output_conflict_preflight_preserves_config(self):
  path=self.root/'.codex/agents/orchestra-worker.toml';path.write_text('unmanaged = true\n')
  payload=self.payload();before=(self.root/'.orchestra/project.toml').read_bytes()
  with self.assertRaises(ValueError): self.service.save(self.root,payload)
  self.assertEqual((self.root/'.orchestra/project.toml').read_bytes(),before);self.assertFalse(self.keys.values)
 def test_drifted_agents_and_missing_manifest_refuse_without_mutation(self):
  for case in ['agents','manifest']:
   with self.subTest(case=case):
    from orchestra_kit.project import sync_project
    sync_project(self.root,KIT)
    if case=='agents':
     p=self.root/'AGENTS.md';p.write_text(p.read_text().replace('## Orchestra','## Changed Orchestra'))
    else: (self.root/'.orchestra/manifest.json').unlink()
    before={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p.name!='settings.lock'}
    with self.assertRaises(ValueError): self.service.save(self.root,self.payload())
    after={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p.name!='settings.lock'}
    self.assertEqual(before,after);self.assertFalse(self.keys.values)
 def test_web_search_capability_reaches_native_runtime(self):
  from orchestra_kit.config import load_config
  from orchestra_kit.providers import resolve_provider_runtime
  payload=self.payload();payload['provider']['capabilities'].append('web_search')
  self.service.save(self.root,payload)
  p=load_config(self.root,KIT).providers['gateway']
  from orchestra_kit.providers import ProviderCredentialService
  runtime=resolve_provider_runtime(p,credential_service=ProviderCredentialService(environment={'GATEWAY_API_KEY':'fake'}))
  self.assertIn('model_providers.gateway.supports_standalone_web_search=true',runtime.argv_config)
 def test_abandoned_lock_file_does_not_permanently_block_save(self):
  (self.root/'.orchestra/settings.lock').write_text('abandoned process')
  result=self.service.save(self.root,self.payload())
  self.assertIn('gateway',result['providers'])

 def test_live_process_lock_blocks_and_process_death_releases_it(self):
  import os,subprocess,sys
  lock=self.root/'.orchestra/settings.lock'
  code='\n'.join(['import sys','from pathlib import Path','from orchestra_kit.settings import _settings_lock','with _settings_lock(Path(sys.argv[1])):', ' print("locked",flush=True)', ' sys.stdin.readline()'])
  child=subprocess.Popen([sys.executable,'-c',code,str(lock)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env={**os.environ,'PYTHONPATH':str(KIT/'src')})
  try:
   self.assertEqual(child.stdout.readline().strip(),'locked')
   with self.assertRaises(SettingsConflict): self.service.save(self.root,self.payload())
  finally:
   child.kill();child.communicate(timeout=3)
  self.assertIn('gateway',self.service.save(self.root,self.payload())['providers'])

 def test_configured_effort_is_not_a_supported_effort_catalog(self):
  options=self.service.get(self.root)['model_options']
  self.assertTrue(options)
  self.assertTrue(all(o['efforts']==[] for o in options if o['source']=='configured_profile'))

 def test_catalog_effort_constraint_and_secret_aliases_fail_before_write(self):
  payload=self.payload();payload['provider']['catalog']=self.catalog();payload['profiles']['cheap']={'model':'custom-small','provider':'gateway','effort':'ultra'}
  with self.assertRaises(ValueError):self.service.save(self.root,payload)
  for key in ['client_secret','secret','token','x-api-key','apiKey','accessToken']:
   payload=self.payload();payload['provider']['catalog']=self.catalog();payload['provider']['catalog']['models'][0]['extra']={key:'demo'}
   with self.subTest(key=key),self.assertRaises(ValueError):self.service.save(self.root,payload)

 def test_incomplete_native_catalog_is_rejected(self):
  payload=self.payload();payload['provider']['catalog']={'models':[{'slug':'tiny','display_name':'Tiny'}]}
  with self.assertRaisesRegex(ValueError,'метаданные'):self.service.save(self.root,payload)

 def test_comments_and_other_tables_are_preserved(self):
  p=self.root/'.orchestra/project.toml';p.write_text(p.read_text().replace('[workflow]','# my important policy\n[workflow]'))
  payload=self.payload();payload['profiles']['strong']['model']='another-model';self.service.save(self.root,payload)
  text=p.read_text();self.assertIn('# my important policy',text);self.assertIn('max_repair_cycles = 2',text);self.assertIn('model = "another-model"',text)

class SettingsHttpTests(unittest.TestCase):
 setUp=SettingsTests.setUp
 payload=SettingsTests.payload
 def test_mutations_require_same_origin_token_and_known_project(self):
  import http.client,threading
  from orchestra_kit.dashboard import Dashboard,make_server
  dashboard=Dashboard([self.root],None,KIT);server=make_server(dashboard,settings_service=self.service)
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  self.addCleanup(server.server_close);self.addCleanup(thread.join,2);self.addCleanup(server.shutdown)
  host,port=server.server_address;conn=http.client.HTTPConnection(host,port,timeout=3)
  self.addCleanup(conn.close)
  pid=dashboard.snapshot()['projects'][0]['id'];path='/api/projects/'+pid+'/settings'
  conn.request('GET','/api/session');response=conn.getresponse();token=json.loads(response.read())['token']
  payload=json.dumps(self.payload())
  for headers in [{'Content-Type':'application/json'},{'Content-Type':'application/json','X-Orchestra-Token':token,'Origin':'http://evil.test'}]:
   conn.request('POST',path,payload,headers);response=conn.getresponse();self.assertEqual(response.status,403);response.read()
  headers={'Content-Type':'application/json','X-Orchestra-Token':token,'Origin':f'http://{host}:{port}'}
  conn.request('POST',path,payload,headers);response=conn.getresponse();body=response.read();self.assertEqual(response.status,200,body);self.assertNotIn(b'unit-test-placeholder',body)
  conn.request('POST','/api/projects/p-unknown/settings',payload,headers);response=conn.getresponse();self.assertEqual(response.status,404);response.read()
