const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/terminal.js','utf8');
const context=vm.createContext({document:{querySelectorAll:()=>[]},mainGuideHTML:()=>''});
vm.runInContext(source.slice(source.indexOf('const esc ='),source.indexOf('const factorNames =')),context);
vm.runInContext(fs.readFileSync('static/mainline.js','utf8'),context);
context.g={board_label:'次主线板块',name:'行业',sector_code:'BK1',rank:2,strength:80,pct:3,
 funds:{net:1e8,date:'2026-11-11',ratio:5,positive_days:2},leaders:[
 {code:'600001',name:'龙头甲',role:'龙头候选',funds:{net:1e7,ratio:3}},
 ...['趋势甲','趋势乙','趋势丙'].map((name,i)=>({code:'60001'+i,name,role:'趋势龙候选',funds:{net:1e7,ratio:2}})),
 ...['补涨甲','补涨乙'].map((name,i)=>({code:'60002'+i,name,role:'补涨观察',funds:{net:1e6,ratio:1}}))],role_gaps:[]};
for(const fn of ['strategyRolesHTML','mainRoleGroupsHTML']){
 const html=vm.runInContext(`${fn}(g)`,context);
 for(const s of context.g.leaders)assert.ok(html.includes(s.name));
 assert.match(html,/其余 2 只趋势候选/);
 assert.match(html,/其余 1 只补涨观察/);
}
assert.match(vm.runInContext('sectorCard(g)',context),/次主线板块/);
context.g.leaders=[];assert.match(vm.runInContext('strategyRolesHTML(g)',context),/待确认/);
console.log('Role groups: board labels, multiple candidates, collapsible extras and empty slots passed.');
context.g.leaders=[{code:'600001',name:'龙头<甲>',role:'龙头候选',role_reason:'涨停池确认<样本>',funds:{net:1e7,date:'2026-11-11'}}];
context.market={groups:[context.g,{...context.g,rank:3,sector_code:'BK2',name:'趋势板块',board_type:'trend',board_label:'趋势板块',leaders:[]}],updated_at:'2026-11-11 15:00:00'};
let commentary=vm.runInContext('mainlineStrategyHTML(market)',context);
assert.match(commentary,/为什么入选次主线板块/);
assert.match(commentary,/行业 · 个股点评/);
assert.match(commentary,/涨停池确认&lt;样本&gt;/);
assert.doesNotMatch(commentary,/class="strategy-board"|strategy-facts|<table>/);
commentary=vm.runInContext('mainlineStrategyHTML(market)',context);
assert.match(commentary,/data-strategy-comment="BK2">/);
assert.match(commentary,/data-strategy-comment="BK1">/);
assert.match(commentary,/未确认/);
console.log('Strategy commentary: selection evidence, escaped reasons, empty roles and simultaneous board columns passed.');
context.readingGroup={date:'2026-11-11',pct:3,breadth:70,strength:87,limit_count:4,max_streak:3,score_coverage:100,stage:'资金与涨停共振',funds:{status:'ok',date:'2026-11-11',net:1e9,ratio:8,positive_days:3},score_parts:{'净流入占比':{score:80,weight:40},'上涨宽度':{score:70,weight:15},'连板高度':{score:60,weight:5}}};
let reading=vm.runInContext('strategySectorReading(readingGroup)',context);
assert.match(reading.summary,/87.0 分/);assert.match(reading.reason,/净流入占比/);assert.match(reading.strategy,/同时满足/);
context.readingGroup.limit_count=1;reading=vm.runInContext('strategySectorReading(readingGroup)',context);assert.match(reading.strategy,/尚未达到/);
context.readingGroup.funds.positive_days=null;reading=vm.runInContext('strategySectorReading(readingGroup)',context);assert.match(reading.strategy,/连续性不足或未核验/);
context.readingGroup.funds.date='2026-11-10';reading=vm.runInContext('strategySectorReading(readingGroup)',context);assert.match(reading.strategy,/日期对齐尚未确认/);
console.log('Sector readings: weighted contributors, resonance, limited breadth and unconfirmed funds passed.');
