import test from 'node:test';
import assert from 'node:assert/strict';
import {usage,attemptTokens} from './metrics.js';
test('incomplete attempts never expose partial token counts as complete',()=>{
 assert.equal(attemptTokens({usage:{complete:false,totals:{total_tokens:123}}}),null);
 assert.equal(attemptTokens({usage:{complete:true,totals:{total_tokens:123}}}),123);
 assert.equal(attemptTokens({usage:{complete:true,total_tokens:0}}),0);
 assert.equal(attemptTokens({}),null);
});
test('known subtotal excludes incomplete executions and reports coverage',()=>{
 const partial={usage:{complete:false,totals:{total_tokens:999}}};
 const complete={usage:{complete:true,totals:{total_tokens:15,input_tokens:10,output_tokens:5}}};
 assert.deepEqual(usage([partial,complete]),{known:1,count:2,total:15,input:10,output:5,cached:null});
 assert.equal(usage([partial]).total,null);
 assert.equal(usage([]).total,null);
});
test('actual zero is preserved and invalid counters remain unknown',()=>{
 assert.equal(usage([{usage:{complete:true,totals:{total_tokens:0}}}]).total,0);
 for(const value of [NaN,Infinity,-1]) assert.equal(attemptTokens({usage:{complete:true,total_tokens:value}}),null);
});
