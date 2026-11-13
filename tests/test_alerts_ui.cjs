const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const {fresh,evaluate}=require('../static/alerts.js');
const values=new Map(),elements=new Map(),items=[{code:'600001',name:'示例甲'}];
const node=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',hidden:true,open:false});return elements.get(id);};
const date='2020-11-11',minute={date,as_of:date+' 15:00',points:[{price:10,average:9,time:'15:00'}],signals:[{time:'15:00',kind:'弱转强'}]};
const funds={status:'ok',date,net:100,ratio:2};
const daily={name:'示例甲',plan:{date,stop:9,target1:13},support:8,funds};
const ctx=vm.createContext({Map,Date,Intl,Number,Math,JSON,Object,Array,PriceAlerts:{fresh,evaluate},state:{mode:'demo',generation:1},stored:(k,v)=>values.has(k)?values.get(k):v,persist:(k,v)=>values.set(k,v),watchItems:()=>items,$:node,esc:v=>String(v??''),fmt:v=>String(v),pct:v=>String(v),saveWatch:()=>{},renderWatch:()=>{},evaluatePlan:()=>({rr:3}),api:async path=>path.includes('/market')?{groups:[{funds,leaders:[{code:'600001',role:'龙头候选'}]}]}:path.includes('/intraday')?minute:daily});
vm.runInContext(fs.readFileSync('static/alerts-ui.js','utf8'),ctx);
(async()=>{
 await vm.runInContext('pollWatchAlerts()',ctx);
 assert.equal(values.get('sy-alert-history-demo').length,1);
 assert.match(node('priceNoticeList').innerHTML,/试仓观察/);
 // History truncation/clearing must not re-trigger a condition already seen today.
 values.set('sy-alert-history-demo',[]);node('priceNotice').hidden=true;
 await vm.runInContext('pollWatchAlerts()',ctx);
 assert.equal(values.get('sy-alert-history-demo').length,0);
 assert.equal(node('priceNotice').hidden,true);
 assert.equal(values.has('sy-alert-history-live'),false);
 console.log('Alert integration: all-watch evaluation, popup evidence, durable deduplication independent of history and mode isolation passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
