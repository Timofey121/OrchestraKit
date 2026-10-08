const pages=new Set(['overview','projects','project','chats','chat','executions','usage','settings','help']);
export function parseRoute(hash='') {
 const params=new URLSearchParams(hash.replace(/^#/,''));
 return {page:pages.has(params.get('page'))?params.get('page'):'overview',project:params.get('project')||'all',chat:params.get('chat')||null};
}
export function routeHash({page,project,chat}) {
 const params=new URLSearchParams({page,project});if(chat)params.set('chat',chat);return '#'+params;
}
export function filterChatWork(tasks,executions,chatId) {
 if(!chatId)return {tasks,executions};
 const selected=tasks.filter(t=>t.chat_id===chatId),ids=new Set(selected.map(t=>t.id));
 return {tasks:selected,executions:executions.filter(e=>ids.has(e.task_id))};
}
