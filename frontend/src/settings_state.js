function canonical(value){
 if(Array.isArray(value))return value.map(canonical);
 if(value&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonical(value[key])]));
 return value;
}
const equal=(a,b)=>JSON.stringify(canonical(a))===JSON.stringify(canonical(b));
const providerFields=p=>({id:p.id,name:p.name,base_url:p.base_url,env_key:p.env_key,capabilities:[...(p.capabilities||[])].sort()});
export function hasUnsavedSettings(data,draft){
 if(!data||data.configured===false)return false;
 for(const key of ['profiles','roles','routing'])if(!equal(data[key]||{},draft[key]||{}))return true;
 if(draft.secret)return true;
 if(!draft.provider)return false;
 const saved=data.providers?.[draft.provider.id];
 if(!saved||!equal(providerFields(saved),providerFields(draft.provider)))return true;
 if(!draft.catalogText?.trim())return false;
 try{return !equal(saved.catalog,JSON.parse(draft.catalogText));}catch{return true;}
}
