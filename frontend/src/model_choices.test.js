import test from 'node:test';import assert from 'node:assert/strict';
import {choicesForProvider,changeModel} from './model_choices.js';
test('choices never cross providers and preserve a manually configured ID',()=>{
 const options=[{id:'a',provider:null},{id:'b',provider:'custom'},{id:'b',provider:'custom'}];
 assert.deepEqual(choicesForProvider(options,'custom','manual').map(o=>o.id),['b','manual']);
 assert.deepEqual(choicesForProvider(options,null,'a').map(o=>o.id),['a']);
});
test('model switch respects declared reasoning levels but unknown levels remain explicit',()=>{
 assert.equal(changeModel({provider:'x',effort:'high'},'m',[{id:'m',efforts:['low','medium']}]).effort,'low');
 assert.equal(changeModel({provider:'x',effort:'high'},'m',[]).effort,'high');
});
