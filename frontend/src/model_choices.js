export function choicesForProvider(options,provider,current){
 const selected=options.filter(o=>(o.provider??null)===(provider??null));
 const unique=[...new Map(selected.map(o=>[o.id,o])).values()];
 if(current&&!unique.some(o=>o.id===current))unique.push({id:current,label:current,efforts:[],source:'manual',provider});
 return unique;
}
export function changeModel(profile,id,options){
 const option=options.find(o=>o.id===id),efforts=option?.efforts||[];
 return {...profile,model:id,effort:efforts.length&&!efforts.includes(profile.effort)?efforts[0]:profile.effort};
}
