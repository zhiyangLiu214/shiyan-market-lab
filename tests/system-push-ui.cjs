const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/system-push.js'),'utf8');
const nodes=new Map(),memory=new Map(),calls=[],listeners={};let permissionCalls=0,failSubscribe=false;
const subscription={toJSON:()=>({endpoint:'https://fcm.googleapis.com/fcm/send/test',keys:{}})};
let browserSubscription=null;
const registration={pushManager:{getSubscription:async()=>browserSubscription,subscribe:async()=>{if(failSubscribe)throw Error('浏览器推送网络不可用');return browserSubscription=subscription;}}};
const notification={permission:'default',requestPermission:async()=>{permissionCalls++;notification.permission='granted';return 'granted';}};
const context={$:id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);},Notification:notification,navigator:{serviceWorker:{register:async()=>registration,ready:Promise.resolve(registration),getRegistration:async()=>registration}},window:{isSecureContext:true,Notification:notification,PushManager:{},addEventListener:(k,f)=>listeners[k]=f,opener:null},location:{host:'example.com',hostname:'example.com'},Uint8Array,atob:s=>Buffer.from(s,'base64').toString('binary'),stored:(k,d)=>memory.has(k)?memory.get(k):d,persist:(k,v)=>memory.set(k,v),localStorage:{getItem:()=>null,setItem(){}},resetTradeNotices(){},renderWatch(){},api:async(url,options)=>{calls.push([url,options]);if(options){const body=JSON.parse(options.body);return body.action==='subscribe'?{id:'device'}:{queued:true};}return {configured:true,public_origin:'https://example.com',public_key:'BA',subscription:memory.has('sy-system-push-id')?{active:1}:null};}};
vm.createContext(context);vm.runInContext(source,context);
(async()=>{
 context.initSystemPush();await context.refreshSystemPush();assert.equal(permissionCalls,0); // no automatic permission prompt
 await context.enableSystemPush();assert.equal(permissionCalls,1);assert.equal(memory.get('sy-system-push-id'),'device');assert.equal(nodes.get('testSystemPush').disabled,false);
 assert.ok(calls.some(([,o])=>o&&JSON.parse(o.body).action==='subscribe'));
 await context.testSystemPush();assert.match(nodes.get('systemPushStatus').textContent,/15秒/);
 context.window.isSecureContext=false;context.renderSystemPush();assert.match(nodes.get('systemPushStatus').textContent,/HTTPS/);
 const sent=[],secure={postMessage:(data,origin)=>sent.push([data,origin])},raw=new Map([['sy-watch-live','[{"code":"600001","cost":10}]'],['unrelated-secret','do-not-copy']]);
 context.window.open=()=>secure;context.localStorage={get length(){return raw.size;},key:i=>[...raw.keys()][i],getItem:k=>raw.has(k)?raw.get(k):null,setItem:(k,v)=>raw.set(k,v)};
 await context.enableSystemPush();listeners.message({data:{type:'shiyan-state-request'},source:secure,origin:'https://evil.test'});assert.equal(sent.length,0);
 listeners.message({data:{type:'shiyan-state-request'},source:secure,origin:'https://example.com'});assert.equal(sent.length,1);assert.ok(sent[0][0].values['sy-watch-live']);assert.equal(sent[0][0].values['unrelated-secret'],undefined);
 const parent={};context.window.opener=parent;raw.clear();listeners.message({data:{type:'shiyan-state-transfer',values:{'sy-watch-live':'[]','other':'secret'}},source:parent,origin:'http://example.com'});assert.equal(raw.get('sy-watch-live'),'[]');assert.equal(raw.has('other'),false);
 context.window.isSecureContext=true;browserSubscription=null;await context.refreshSystemPush();assert.equal(nodes.get('testSystemPush').disabled,true);
 failSubscribe=true;await context.enableSystemPush();assert.match(nodes.get('systemPushStatus').textContent,/网络不可用/);
 console.log('系统通知界面：手势授权、HTTPS检测、订阅与失效状态、延迟测试、网络失败提示通过。');
})().catch(e=>{console.error(e);process.exitCode=1;});
