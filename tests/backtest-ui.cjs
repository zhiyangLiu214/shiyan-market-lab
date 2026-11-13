const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const context={
 document:{addEventListener(){}},
 esc:s=>String(s??'').replaceAll('<','&lt;'),
 fmt:n=>n==null?'—':String(n),pct:n=>n==null?'—':n+'%',sign:n=>n>=0?'up':'down',
 paperSpark:rows=>'<chart>'+rows.length+'</chart>'
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/backtest.js','utf8'),context);
const result={name:'<策略>',complete:false,return_pct:null,nav:null,drawdown:1,realized:0,fees:5,trades:[],positions:[],selections:[],skipped:[],equity:[],errors:[{date:'2026-10-09',code:'600001',reason:'分钟缺失'}]};
const html=context.backtestResultHTML({start:'2026-09-30',end:'2026-11-13',status:'partial',progress:'结束',results:[result],errors:[]});
assert.match(html,/已覆盖的样本/);
assert.match(html,/收益尚未核算完整/);
assert.match(html,/&lt;策略>/);
assert.match(html,/分钟缺失/);
assert.doesNotMatch(html,/>0%/);
const downloadable=context.backtestResultHTML({id:'a'.repeat(32),xlsx_available:true,results:[]});
assert.match(downloadable,/format=xlsx/);
assert.match(downloadable,/format=json/);
assert.match(downloadable,/独立账本/);
const fees=context.backtestResultHTML({results:[],fee_schedule:{commission_rate:.0001,minimum_commission:1,transfer_rate:.00001,sell_stamp_rate:.0005}});
assert.match(fees,/双向万1/);
assert.match(fees,/每笔最低1元/);
console.log('历史回溯：缺口、未完成收益和转义显示通过。');
