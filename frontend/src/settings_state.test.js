import test from 'node:test';
import assert from 'node:assert/strict';
import {hasUnsavedSettings} from './settings_state.js';
const saved={configured:true,profiles:{cheap:{model:'mini',effort:'low',provider:null}},roles:{worker:'cheap'},routing:{enabled:true,profile_order:['cheap','strong']},providers:{gateway:{id:'gateway',name:'Gateway',base_url:'http://localhost:9000',env_key:null,capabilities:['function'],catalog:{models:[]}}}};
const draft=()=>({profiles:structuredClone(saved.profiles),roles:{...saved.roles},routing:structuredClone(saved.routing),provider:null,secret:'',catalogText:''});
test('settings dirty state detects real persisted changes and clears after saving',()=>{
 assert.equal(hasUnsavedSettings(saved,draft()),false);
 for(const change of [d=>d.profiles.cheap.model='large',d=>d.roles.worker='strong',d=>d.routing.enabled=false,d=>d.routing.profile_order.reverse()]){const d=draft();change(d);assert.equal(hasUnsavedSettings(saved,d),true);assert.equal(hasUnsavedSettings({...saved,profiles:d.profiles,roles:d.roles,routing:d.routing},d),false)}
 assert.equal(hasUnsavedSettings(null,draft()),false);
 assert.equal(hasUnsavedSettings({configured:false},{...draft(),routing:{enabled:true,profile_order:[]}}),false);
});
test('provider editing warns for changes, catalogs and credentials but not opening an unchanged provider',()=>{
 const d=draft();const {catalog,...provider}=saved.providers.gateway;d.provider={...provider};d.catalogText=JSON.stringify(catalog,null,2);
 assert.equal(hasUnsavedSettings(saved,d),false);d.secret='a-test-draft-value';assert.equal(hasUnsavedSettings(saved,d),true);d.secret='';d.provider.name='Changed';assert.equal(hasUnsavedSettings(saved,d),true);
 d.provider={...provider};d.catalogText='{"models":[{"slug":"other"}]}';assert.equal(hasUnsavedSettings(saved,d),true);
 d.provider={id:'new'};assert.equal(hasUnsavedSettings(saved,d),true);
});
