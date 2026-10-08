const plural = new Intl.PluralRules('ru-RU');
const number = new Intl.NumberFormat('ru-RU');
const valid = n => Number.isInteger(n) && n >= 0;
export function noun(n,forms){return forms[{one:0,few:1,many:2,other:2}[plural.select(valid(n)?n:0)]];}
export function count(n,forms){return (valid(n)?number.format(n):'—')+' '+noun(n,forms);}
export function recordedWork(n){
  return count(n,['задача','задачи','задач'])+' '+(plural.select(valid(n)?n:0)==='one'?'отмечена':'отмечены')+' «В работе»';
}
export function projectActivity(project,chats){
  const own=chats.filter(c=>c.project_id===project.id&&!c.archived);
  const active=own.filter(c=>c.status==='running').sort((a,b)=>String(b.updated_at||'').localeCompare(String(a.updated_at||'')));
  if(active.length)return {state:'running',label:count(active.length,['чат','чата','чатов'])+' '+(plural.select(active.length)==='one'?'отвечает':'отвечают'),chat:active[0],description:active[0].last_activity||'В чате идёт ответ. Описание шага пока не записано.'};
  if(!own.length)return {state:'empty',label:'Открытые чаты пока не найдены',description:'В проекте пока нет доступных неархивных чатов Codex.'};
  if(own.some(c=>!['idle','stopped'].includes(c.status)))return {state:'unknown',label:'Нет свежего подтверждения работы',description:'Текущее состояние части чатов неизвестно. История доступна в проекте.'};
  return {state:'idle',label:'Сейчас никто не отвечает',description:'По последним событиям ответы завершены или остановлены.'};
}
export function projectDataReady(project,details){
  return project==='all'||details.some(d=>d.project?.id===project);
}
export function chatWorkSummary(tasks,pending,executions){
  const parts=[];
  if(pending>0)parts.push(recordedWork(pending));
  else if(tasks>0)parts.push(count(tasks,['связанная задача','связанные задачи','связанных задач']));
  if(executions>0)parts.push(count(executions,['незавершённый запуск','незавершённых запуска','незавершённых запусков']));
  return parts.join(' · ');
}
