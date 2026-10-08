import test from 'node:test';
import assert from 'node:assert/strict';
import {filterChatWork,parseRoute,routeHash} from './navigation.js';
test('chat workspace never includes another chat task or an unbound execution',()=>{
 const tasks=[{id:'one',chat_id:'a'},{id:'two',chat_id:'b'},{id:'legacy'}];const executions=[{id:'e1',task_id:'one'},{id:'e2',task_id:'two'},{id:'legacy'}];
 assert.deepEqual(filterChatWork(tasks,executions,'a'),{tasks:[tasks[0]],executions:[executions[0]]});
 assert.deepEqual(filterChatWork(tasks,executions,'missing'),{tasks:[],executions:[]});
});
test('project and chat links survive reload and back navigation',()=>{
 const state={page:'chat',project:'p-abc',chat:'chat/with special chars'};assert.deepEqual(parseRoute(routeHash(state)),state);
 assert.deepEqual(parseRoute('#page=invalid'),{page:'overview',project:'all',chat:null});
});
