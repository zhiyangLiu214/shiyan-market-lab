const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/strategy-sim.js'),'utf8');
const boardNodes=['main','chinext','star'].map(value=>({value,checked:false}));
const patternNodes=[{value:'vwap_cross',checked:false}];
const elements=Object.fromEntries(['name','description','formula','ranking','ranking_mode','pool','codes','buy_time','take_profit','stop_loss','position_pct','max_exposure','max_positions'].map(k=>[k,{value:''}]));
const form={elements,querySelectorAll(selector){const nodes=selector.includes('boards')?boardNodes:patternNodes;return selector.endsWith(':checked')?nodes.filter(n=>n.checked):nodes;}};
const codeRow={},rankingRow={};const context={$:id=>id==='simForm'?form:id==='simRankingFormula'?rankingRow:codeRow,esc:s=>String(s).replaceAll('<','&lt;'),fmt:x=>String(x)};
vm.createContext(context);vm.runInContext(source,context);
context.simFill();assert.equal(elements.pool.value,'market');assert.equal(codeRow.hidden,true);assert.ok(boardNodes.every(b=>b.checked));assert.equal(elements.ranking_mode.value,'composite');assert.equal(rankingRow.hidden,true);assert.equal(elements.ranking.required,false);
boardNodes[0].checked=false;assert.equal(JSON.stringify(context.simConfig().boards),JSON.stringify(['chinext','star']));
context.simFill({pool:'custom',boards:['star'],formula:'C>0'});assert.equal(elements.ranking_mode.value,'formula');assert.equal(rankingRow.hidden,false);assert.equal(elements.ranking.required,true);assert.equal(codeRow.hidden,false);assert.equal(JSON.stringify(context.simConfig().boards),'["star"]');
const html=context.simMarketScan({status:'running',reason:'扫描中',source:'交易所',started:'09:31',checked:'09:32',total:70,done:2,failed:0,excluded:0,matched:1,rows:[{code:'688001',name:'<测试>',matched:true,data_ok:true,rank:1}]});
assert.match(html,/688001/);assert.match(html,/&lt;测试>/);assert.match(html,/范围总数/);assert.match(html,/全部公式入选股票/);
console.log('市场范围界面：默认三市场、勾选保存、历史策略回填、完整扫描结果和转义通过。');

const scoreHtml=context.simScoreEvidence({ranking_mode:'composite',scores:[85,75,100],score_version:'daily-v1.0',score_reasons:['<突破>']});assert.match(scoreHtml,/结构 85/);assert.match(scoreHtml,/趋势 75/);assert.match(scoreHtml,/量价 100/);assert.match(scoreHtml,/&lt;突破>/);
assert.equal(context.simScoreEvidence({ranking_mode:'formula'}),'');
