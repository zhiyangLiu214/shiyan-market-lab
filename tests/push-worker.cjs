const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const handlers={},shown=[],opened=[];let focused=false;
const self={location:{origin:'https://example.com'},addEventListener:(k,f)=>handlers[k]=f,skipWaiting(){},registration:{showNotification:async(t,o)=>shown.push([t,o])},clients:{claim:async()=>{},matchAll:async()=>[],openWindow:async u=>opened.push(u)}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/push-worker.js'),'utf8'),{self,URL});
(async()=>{
 let pending;handlers.push({data:{json:()=>({id:'order:1',title:'买入已模拟成交',body:'真实模拟记录',url:'/?page=sim'})},waitUntil:p=>pending=p});await pending;assert.equal(shown[0][1].tag,'order:1');assert.equal(shown[0][1].requireInteraction,true);assert.equal(shown[0][1].renotify,false);
 handlers.notificationclick({notification:{close(){},data:{url:'https://evil.test'}},waitUntil:p=>pending=p});await pending;assert.equal(opened[0],'https://example.com/');
 self.clients.matchAll=async()=>[{url:'https://example.com/',navigate:async()=>{},focus:async()=>focused=true}];handlers.notificationclick({notification:{close(){},data:{url:'/?mode=live&page=sim'}},waitUntil:p=>pending=p});await pending;assert.equal(focused,true);
 console.log('后台推送：离开页面仍可展示、事件标识去重、点击聚焦与同源地址约束通过。');
})().catch(e=>{console.error(e);process.exitCode=1;});
