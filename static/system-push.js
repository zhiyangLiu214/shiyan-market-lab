'use strict';
let systemPushState=null,secureWindow=null,systemPushBusy=false,systemPushError='';
const pushIdKey='sy-system-push-id';
function pushPublicKey(value){const raw=atob(value.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-value.length%4)%4));return Uint8Array.from(raw,c=>c.charCodeAt(0));}
function renderSystemPush(){
 const info=systemPushState,button=$('enableSystemPush'),status=$('systemPushStatus');$('testSystemPush').disabled=true;
 if(!window.isSecureContext){status.textContent='桌面通知需要HTTPS；点击保留自选设置并打开安全地址。';button.textContent='打开HTTPS启用桌面通知';button.disabled=!info?.public_origin;return;}
 if(!('Notification' in window)||!('serviceWorker' in navigator)||!('PushManager' in window)){status.textContent='此浏览器不支持系统推送，请使用Chrome、Firefox或Safari。';button.disabled=true;return;}
 button.disabled=systemPushBusy||!info?.configured;
 if(!info?.configured){status.textContent=info?.worker_error||'系统通知通道尚未就绪';return;}
 if(Notification.permission==='denied'){status.textContent='通知权限已被阻止，请在地址栏的网站设置中允许通知。';button.textContent='重新检查通知权限';return;}
 if(Notification.permission==='granted'&&info.subscription?.active){status.textContent=info.subscription.last_error||'系统通知已订阅：切到其他应用也可提醒，无需网页置顶。';button.textContent='重新核对系统通知';$('testSystemPush').disabled=false;}
 else{status.textContent='需要一次浏览器授权；用于真实模式五个模拟账户的买卖提醒。';button.textContent='启用系统桌面通知';}
 if(info.worker_error)status.textContent=info.worker_error;
 if(systemPushError)status.textContent=systemPushError;
}
async function refreshSystemPush(){try{const id=stored(pushIdKey,'');systemPushState=await api('/api/terminal/system-push'+(id?'?id='+encodeURIComponent(id):''));if(window.isSecureContext&&'serviceWorker' in navigator&&'Notification' in window&&Notification.permission==='granted'){const r=await navigator.serviceWorker.getRegistration('/');if(!r||!await r.pushManager.getSubscription())systemPushState.subscription=null;}renderSystemPush();}catch(e){$('systemPushStatus').textContent='系统通知状态读取失败：'+e.message;}}
async function enableSystemPush(){
 if(systemPushBusy)return;
 if(!window.isSecureContext){if(systemPushState?.public_origin)secureWindow=window.open(systemPushState.public_origin+'/?page=sim','shiyan-secure');return;}
 if(!systemPushState?.configured)return;
 systemPushBusy=true;systemPushError='';renderSystemPush();
 try{
  const permission=await Notification.requestPermission();if(permission!=='granted')throw Error('未获得通知权限，请在网站设置中允许通知');
  const registration=await navigator.serviceWorker.register('/service-worker.js',{scope:'/'});await navigator.serviceWorker.ready;
  let subscription=await registration.pushManager.getSubscription();
  if(subscription&&systemPushState.subscription?.active===0){await subscription.unsubscribe();subscription=null;}
  subscription=subscription||await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:pushPublicKey(systemPushState.public_key)});
  const result=await api('/api/terminal/system-push',{method:'POST',body:JSON.stringify({action:'subscribe',subscription:subscription.toJSON()})});persist(pushIdKey,result.id);await refreshSystemPush();
 }catch(e){systemPushError='系统通知未启用：'+e.message;}
 finally{systemPushBusy=false;renderSystemPush();}
}
async function testSystemPush(){try{await api('/api/terminal/system-push',{method:'POST',body:JSON.stringify({action:'test',id:stored(pushIdKey,'')})});$('systemPushStatus').textContent='15秒后发送系统测试通知，现在可以切到其他应用查看；不会下单。';}catch(e){$('systemPushStatus').textContent='测试通知未发送：'+e.message;}}
function initSystemPush(){
 $('enableSystemPush').onclick=enableSystemPush;$('testSystemPush').onclick=testSystemPush;
 window.addEventListener('message',event=>{
  if(event.data?.type==='shiyan-state-request'&&event.source===secureWindow&&event.origin===systemPushState?.public_origin){
   const values={};for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);if(k?.startsWith('sy-'))values[k]=localStorage.getItem(k);}
   event.source.postMessage({type:'shiyan-state-transfer',values},event.origin);
  }else if(event.data?.type==='shiyan-state-transfer'&&event.source===window.opener&&event.origin==='http://'+location.host){
   for(const [k,v] of Object.entries(event.data.values||{}))if(k.startsWith('sy-')&&typeof v==='string'){
    const current=localStorage.getItem(k);
    if(current===null)localStorage.setItem(k,v);
    else if(k.startsWith('sy-operation-notices-v1-')){
     try{const old=JSON.parse(v),next=JSON.parse(current),ids=new Set(),queue=[];for(const n of [...(old.queue||[]),...(next.queue||[]).filter(n=>n.seq>(old.cursors?.[n.stream]||0))])if(!ids.has(n.id)){ids.add(n.id);queue.push(n);}const cursors={...old.cursors};for(const [stream,seq] of Object.entries(next.cursors||{}))cursors[stream]=Math.max(cursors[stream]||0,seq);localStorage.setItem(k,JSON.stringify({cursors,queue}));}catch{}
    }
   }
   resetTradeNotices();renderWatch();
  }
 });
 if(window.isSecureContext&&window.opener)window.opener.postMessage({type:'shiyan-state-request'},'http://'+location.host);
 refreshSystemPush();
}
