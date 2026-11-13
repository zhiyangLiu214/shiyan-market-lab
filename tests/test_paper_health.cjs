const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ctx=vm.createContext({});vm.runInContext(fs.readFileSync('static/paper.js','utf8'),ctx);
const s={as_of:'2026-11-13 13:30:00',accounts:[{id:'main',orders:[],trades:[]},{id:'scan',orders:[{created:'2026-11-12 10:00:00'}],trades:[]}],pool_diagnostics:[
 {account:'main',at:'2026-11-13 13:28:00',state:'ready',count:0,reason:'旧状态'},
 {account:'main',at:'2026-11-13 13:29:00',state:'unavailable',count:0,reason:'备用资金只读'},
 {account:'scan',at:'2026-11-13 13:29:00',state:'ready',count:0,reason:'无85分候选'}]};
let r=ctx.paperExecutionChecks(s);
assert.equal(r[0].blocked,true);assert.match(r[0].label,/数据故障/);assert.equal(r[0].reason,'备用资金只读');
assert.equal(r[1].blocked,false);assert.match(r[1].label,/本轮无候选/);assert.equal(r[1].orders,0);
s.pool_diagnostics[2].state='partial';r=ctx.paperExecutionChecks(s);assert.match(r[1].label,/无法确认/);assert.equal(r[1].blocked,true);
s.pool_diagnostics[2].state='ready';s.pool_diagnostics[2].count=2;
s.candidates={scan:{items:[{name:'样本一',data_error:true,decision:'个股资金不可用'},{name:'样本二',data_error:true,decision:'资金日期过旧'}]}};
r=ctx.paperExecutionChecks(s);assert.match(r[1].label,/候选数据故障/);assert.match(r[1].reason,/2\/2只候选/);assert.match(r[1].reason,/个股资金不可用/);
s.candidates.scan.items[1].data_error=false;r=ctx.paperExecutionChecks(s);assert.match(r[1].label,/部分候选/);
s.candidates.scan.items.forEach(c=>c.data_error=false);r=ctx.paperExecutionChecks(s);assert.equal(r[1].blocked,false);assert.match(r[1].label,/候选已就绪/);
s.pool_diagnostics=[];r=ctx.paperExecutionChecks(s);assert.match(r[1].label,/等待/);assert.equal(r[1].count,null);
console.log('模拟诊断：数据故障、空池、不完整和加载中明确区分；按最新核对记录及当日成交统计。');
