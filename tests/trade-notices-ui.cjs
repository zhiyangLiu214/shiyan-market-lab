const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/trade-notices.js'),'utf8');
const memory=new Map(),nodes=new Map(),state={mode:'live',generation:1},toasts=[];
const $=id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);};
const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const events=Array.from({length:7},(_,i)=>({id:'paper:'+(i+1),seq:i+1,stream:'paper',origin:'A号·今日主线',phase:'operation',title:i===1?'买入已模拟成交':'买入委托已提交',text:'<未成交或真实状态>',name:'<股票>',code:'600001',at:'2026-11-14 10:00:00',source:'auto'}));
let fail=false,delay=null,calls=0,pageSize=100;
const context={$,state,esc,toast:x=>toasts.push(x),stored:(k,d)=>memory.has(k)?JSON.parse(memory.get(k)):d,localStorage:{setItem:(k,v)=>memory.set(k,v)},window:{addEventListener(){}},api:async url=>{calls++;if(fail)throw Error('断线');if(delay)await delay;const c=JSON.parse(decodeURIComponent(url.split('&cursors=')[1]));const fresh=events.filter(e=>e.seq>(c.paper||0)),page=fresh.slice(0,pageSize);return {events:page,cursors:{paper:page.length?page.at(-1).seq:c.paper||0},has_more:fresh.length>pageSize};}};
vm.createContext(context);vm.runInContext(source,context);
(async()=>{
 context.initTradeNotices();await context.pollTradeNotices();assert.equal($('simNotice').hidden,false);assert.match($('simNoticeTitle').textContent,/7/);
 assert.match($('simNoticeItems').innerHTML,/买入已模拟成交/);assert.match($('simNoticeItems').innerHTML,/&lt;股票>/);assert.doesNotMatch($('simNoticeItems').innerHTML,/<股票>/);
 assert.equal(($('simNoticeItems').innerHTML.match(/class="operation-notice"/g)||[]).length,3);
 await context.pollTradeNotices();assert.match($('simNoticeTitle').textContent,/7/);
 context.acknowledgeTradeNotices();assert.match($('simNoticeTitle').textContent,/4/);
 context.resetTradeNotices();assert.match($('simNoticeTitle').textContent,/4/); // refresh preserves the undisplayed four
 fail=true;await context.pollTradeNotices();assert.match($('simNoticeTitle').textContent,/4/);assert.equal(toasts.length,1);fail=false;
 context.acknowledgeTradeNotices(true);assert.equal($('simNotice').hidden,true);await context.pollTradeNotices();assert.equal($('simNotice').hidden,true);
 // A stale live response cannot render in demo mode.
 let release;delay=new Promise(r=>release=r);const pending=context.pollTradeNotices();state.mode='demo';state.generation++;context.resetTradeNotices();release();await pending;assert.equal($('simNotice').hidden,true);delay=null;
 state.mode='live';state.generation++;context.resetTradeNotices();assert.equal($('simNotice').hidden,true);
 // Corrupt persisted cursor fields are repaired without hiding pending messages.
 memory.set('sy-operation-notices-v1-demo',JSON.stringify({cursors:{paper:-2,bad:'x'},queue:[]}));assert.equal(JSON.stringify(context.readTradeNoticeState('demo').cursors),'{}');
 memory.delete('sy-operation-notices-v1-live');context.resetTradeNotices();pageSize=2;await context.pollTradeNotices();assert.match($('simNoticeTitle').textContent,/7/);assert.equal(JSON.parse(memory.get('sy-operation-notices-v1-live')).cursors.paper,7);
 console.log('操作弹窗：全部排队、逐组已读、刷新续读、断线保留、模式隔离、成交与委托区分、转义通过。');
})().catch(e=>{console.error(e);process.exitCode=1;});
