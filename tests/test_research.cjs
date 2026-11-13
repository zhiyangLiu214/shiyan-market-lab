const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const context=vm.createContext({});
vm.runInContext(fs.readFileSync('static/research.js','utf8'),context);
const p={reference:10,stop:9,target1:12,date:'2026-11-11',as_of:'09:40'};
function check(points,date='2026-11-11'){context.p=p;context.m={date,points:points.map(([time,price])=>({time,price}))};return vm.runInContext('evaluatePlan(p,m)',context);}
assert.equal(check([['09:30',8],['09:40',10],['09:41',11]]).rr,.5); // ignores pre-signal low
assert.match(check([['09:41',12],['09:42',10]]).status,/触及首压力/); // target hit remains visible after pullback
assert.equal(check([['09:41',12],['09:42',10]]).rr,null); // completed plans do not become fresh entries
assert.equal(check([['09:41',13]]).rr,null); // old positive plan ratio cannot advertise a fresh entry
assert.match(check([['09:41',13]]).status,/越过/);
assert.match(check([['09:41',8],['09:42',10]]).status,/止损/);
assert.match(check([['09:41',10]],'2026-11-10').status,/早于/);
assert.match(check([['09:41',10]],'2026-11-12').status,/跨日/);
context.m=null;assert.equal(vm.runInContext('evaluatePlan(p,m).current',context),null);
console.log('Plan evaluation: timestamp, target/stop history, remaining ratio, missing and cross-date data passed.');
