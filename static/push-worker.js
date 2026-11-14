'use strict';
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('push',event=>{
 let data;try{data=event.data.json();if(!data||typeof data!=='object')throw Error('invalid payload');}catch{data={title:'势研 · 模拟提醒',body:'收到一条操作提醒，请打开网站查看详情。'};}
 event.waitUntil(self.registration.showNotification(data.title||'势研 · 模拟提醒',{body:data.body||'',tag:data.id||'shiyan-notice',renotify:false,requireInteraction:true,icon:'/static/favicon.svg',data:{url:data.url||'/?mode=live&page=sim'}}));
});
self.addEventListener('notificationclick',event=>{
 event.notification.close();
 event.waitUntil((async()=>{
  let url=new URL(event.notification.data?.url||'/',self.location.origin);if(url.origin!==self.location.origin)url=new URL('/',self.location.origin);
  const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
  const found=windows.find(c=>new URL(c.url).origin===self.location.origin);
  if(found){await found.navigate(url.href);return found.focus();}return self.clients.openWindow(url.href);
 })());
});
