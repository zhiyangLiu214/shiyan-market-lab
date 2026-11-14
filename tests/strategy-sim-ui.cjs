const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/terminal.js'),'utf8');
const mode=source.slice(source.indexOf('async function changeMode('),source.indexOf('async function refresh('));
const status=source.slice(source.indexOf('function renderAutoStatus('),source.indexOf('realtimeLoop=new RefreshLoop'));
(async()=>{
 const nodes=new Map();let resets=0,persisted=null,updates=0;
 const state={page:'sim',generation:3,details:new Map(),selected:{},detailTokens:{watch:0,scan:0}};
 const context={state,history:{replaceState(){}},$:id=>{if(!nodes.has(id))nodes.set(id,{close(){}});return nodes.get(id);},
   clearStockSearch(){},persist:(k,v)=>persisted=v,resetStrategySim(){resets++;},resetTradeNotices(){},updateMode(){updates++;},renderWatch(){},renderStrategy(){},refresh:async()=>{},
   loadPaper(){},paperState:null,alertNotice:[],realtimeLoop:{reset(){},tick(){}},intervalMs:()=>30000};
 vm.createContext(context);vm.runInContext(mode+status,context);
 await context.changeMode('demo');
 assert.equal(state.generation,4);assert.equal(resets,1);assert.equal(persisted,'demo');assert.equal(updates,1);
 context.renderAutoStatus();assert.match(nodes.get('autoStatus').textContent,/策略引擎/);
 console.log('策略模拟界面：数据模式完整重置、独立刷新状态通过。');
})().catch(error=>{console.error(error);process.exitCode=1;});
