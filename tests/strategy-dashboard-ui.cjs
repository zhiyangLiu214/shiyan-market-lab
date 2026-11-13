const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/strategy-sim.js'),'utf8');
const elements=Object.fromEntries(['name','description','formula','ranking','ranking_mode','pool','codes','buy_time','take_profit','stop_loss','position_pct','max_exposure','max_positions'].map(k=>[k,{value:''}]));
const boards=['main','chinext','star'].map(value=>({value,checked:true})),patterns=[{value:'vwap_cross',checked:true}],nodes=new Map();
const form={elements,querySelectorAll(s){const all=s.includes('boards')?boards:patterns;return s.endsWith(':checked')?all.filter(x=>x.checked):all;}};
const get=id=>{if(id==='simForm')return form;if(!nodes.has(id))nodes.set(id,{innerHTML:'',querySelectorAll:()=>[]});return nodes.get(id);};
const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const items=['a','b','c','d'].map((id,i)=>({id,enabled:i<3,config:{name:'策略'+id,description:'',formula:'C>0',ranking:'C',pool:'custom',codes:['600001'],boards:['main'],buy_time:'14:30',take_profit:10,stop_loss:5,max_positions:5},status:'已保存',ledger:{accounts:[]},result:{},risk_lines:[],daily_runs:[]}));
const calls=[],state={mode:'live',generation:1,page:'sim'};let failId=null,failSave=false;
const context={$,state,esc,fmt:x=>String(x),pct:x=>String(x),stored:(_k,d)=>d,persist(){},api:async(url,options)=>{
 calls.push([url,options]);if(options){const body=JSON.parse(options.body);if(body.action==='save'){if(failSave)throw Error('保存失败');return items[3];}return {...items.find(x=>x.id===body.id),enabled:body.enabled};}
 if(url.includes('&id=')){const id=url.split('&id=')[1];if(id===failId)throw Error('网络中断');return items.find(x=>x.id===id);}
 return {items,notices:[]};
}};
function $(id){return get(id);}
vm.createContext(context);vm.runInContext(source,context);
(async()=>{
 assert.equal(JSON.stringify(context.simChooseSlots(items,['b','b','deleted'])),'["b","a","c"]');
 await context.loadStrategySim();const html=get('simResults').innerHTML;
 assert.equal((html.match(/class="sim-slot"/g)||[]).length,3);for(const id of ['a','b','c'])assert.ok(html.includes(`data-sim-card="${id}"`));
 assert.match(get('simList').innerHTML,/合计5个/);assert.equal(calls.filter(([u])=>u.includes('&id=')).length,3);
 // A card action explicitly targets B, regardless of which rule was edited.
 await context.simAction('toggle','b');const post=calls.find(([,o])=>o&&JSON.parse(o.body).action==='toggle');assert.equal(JSON.parse(post[1].body).id,'b');assert.equal(JSON.parse(post[1].body).enabled,false);
 failId='b';await context.loadStrategySim();assert.match(get('simResults').innerHTML,/网络中断/);assert.match(get('simResults').innerHTML,/策略b/);failId=null;
 context.simFill();get('simEditor').open=true;await context.simAction('save');assert.equal(get('simEditor').open,false);assert.ok(get('simResults').innerHTML.includes('data-sim-card="d"'));
 failSave=true;get('simEditor').open=true;elements.name.value='未保存草稿';await context.simAction('save');assert.equal(get('simEditor').open,true);assert.equal(elements.name.value,'未保存草稿');assert.match(get('simMessage').textContent,/保存失败/);
 const isolated=items[0];isolated.ledger.accounts=[{nav:100120,pnl:120,exposure:10,positions:[{code:'600001',name:'甲持仓',qty:100,available:100,average_cost:10,value:1100,unrealized:100}],orders:[],trades:[{at:'14:31',name:'甲成交',side:'sell',qty:100,price:11,fees:5,realized:95}]}];
 const card=context.simCard(isolated);assert.match(card,/甲持仓/);assert.match(card,/甲成交/);assert.match(card,/data-sim-id="a"/);assert.doesNotMatch(card,/data-sim-id="b"/);
 console.log('三策略对照：三栏并列、按ID独立操作、保存收起、失败留草稿、部分刷新失败保留结果和持仓成交归属通过。');
})().catch(e=>{console.error(e);process.exitCode=1;});
