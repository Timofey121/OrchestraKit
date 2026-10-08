import React,{useState} from 'react';
import {Dropdown} from './controls.jsx';
export default function ModelPicker({label,value,options,onChange,disabled}){
 const [manual,setManual]=useState(false);
 const selected=manual?'\0manual':value;
 return <div className="model-picker"><Dropdown label={label} value={selected} disabled={disabled} options={[...options.map(o=>({value:o.id,label:o.label||o.id,description:o.source==='manual'?'ID введён вручную':o.source==='configured_profile'?'Из настроек проекта':'Из локального каталога'})),{value:'\0manual',label:'Ввести другой ID модели'}]} onChange={id=>{if(id==='\0manual'){setManual(true);return;}setManual(false);onChange(id);}}/>{manual&&<input required aria-label={label+' — свой ID'} disabled={disabled} maxLength={300} value={value} placeholder="Точный ID из документации провайдера" onChange={e=>onChange(e.target.value)}/>}</div>;
}
