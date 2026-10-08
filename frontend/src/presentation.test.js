import test from 'node:test';
import assert from 'node:assert/strict';
import {count,recordedWork,projectActivity,projectDataReady} from './presentation.js';

test('Russian counts handle singular, few, many and unknown values',()=>{
  for(const [n,text] of [[0,'0 чатов'],[1,'1 чат'],[2,'2 чата'],[5,'5 чатов'],[11,'11 чатов'],[21,'21 чат'],[112,'112 чатов']])
    assert.equal(count(n,['чат','чата','чатов']),text);
  assert.equal(count(null,['чат','чата','чатов']),'— чатов');
  assert.equal(recordedWork(1),'1 задача отмечена «В работе»');
  assert.equal(recordedWork(2),'2 задачи отмечены «В работе»');
});

test('recorded running tasks never invent live activity or cross-project chat links',()=>{
  const p={id:'a',counts:{persisted_running_tasks:5}};
  assert.equal(projectActivity(p,[{id:'other',project_id:'b',status:'running'}]).state,'empty');
  assert.equal(projectActivity(p,[{id:'old',project_id:'a',status:'unknown'}]).state,'unknown');
  assert.equal(projectActivity(p,[{id:'archived',project_id:'a',status:'running',archived:true}]).state,'empty');
});

test('most recent observed active chat provides a direct action; idle is explicit',()=>{
  const p={id:'a'};
  const chats=[{id:'old',project_id:'a',status:'running',updated_at:'2026-10-01'},
    {id:'new',project_id:'a',status:'running',updated_at:'2026-10-05',last_activity:'Проверяю тесты'}];
  const activity=projectActivity(p,chats);
  assert.equal(activity.chat.id,'new');assert.equal(activity.description,'Проверяю тесты');
  assert.equal(activity.label,'2 чата отвечают');
  const many=Array.from({length:21},(_,id)=>({id:String(id),project_id:'a',status:'running'}));
  assert.equal(projectActivity(p,many).label,'21 чат отвечает');
  assert.equal(projectActivity(p,[{project_id:'a',status:'idle'}]).state,'idle');
  assert.equal(projectActivity(p,[{project_id:'a',status:'idle'},{project_id:'a',status:'unknown'}]).state,'unknown');
});

test('project details must load before showing empty tasks or zero counts',()=>{
  assert.equal(projectDataReady('a',[]),false);
  assert.equal(projectDataReady('a',[{project:{id:'b'},tasks:[]}]),false);
  assert.equal(projectDataReady('a',[{project:{id:'a'},tasks:[]}]),true);
  assert.equal(projectDataReady('all',[]),true);
});

test('chat summaries omit empty work counters but retain relevant work',async()=>{
  const {chatWorkSummary}=await import('./presentation.js');
  assert.equal(chatWorkSummary(0,0,0),'');
  assert.equal(chatWorkSummary(2,1,0),'1 задача отмечена «В работе»');
  assert.equal(chatWorkSummary(2,0,1),'2 связанные задачи · 1 незавершённый запуск');
});
