'use strict';
const alertQuotes=new Map(),alertDailyCache=new Map();
let alertEditing=null,alertNotice=[],alertRoleMarket=null;
function alertConfig(item){return {cost:Number.isFinite(item.cost)&&item.cost>0?item.cost:null,plan:item.holdingPlan||null};}
function alertHistory(){const rows=stored(`sy-alert-history-${state.mode}`,[]);return Array.isArray(rows)?rows:[];}
function watchAlertControls(item){const c=alertConfig(item),q=alertQuotes.get(state.mode+':'+item.code);return `<div class="watch-alert-meta"><span>${c.cost?`持仓观察 · 成本 ${fmt(c.cost)}${q&&PriceAlerts.fresh(q,Date.now(),state.mode==='demo')?' · 浮动 '+pct((q.price/c.cost-1)*100):''}`:'未持仓观察 · 自动识别机会与风险'}</span><button class="text-button" data-action="alert-edit" data-code="${esc(item.code)}">${c.cost?'修改成本':'填写成本'}</button></div>`;}
function openAlertEditor(code){const item=watchItems().find(s=>s.code===code);if(!item)return;alertEditing={code,mode:state.mode};$('alertEditorTitle').textContent=`${item.name||code} · 持仓成本`;$('alertCost').value=alertConfig(item).cost??'';$('alertEditorError').textContent='';$('alertEditor').showModal();}
async function getAlertDaily(code,mode){const key=mode+':'+code,cached=alertDailyCache.get(key);if(cached&&Date.now()-cached.time<60000)return cached.value;const value=await api(`/api/terminal/stock/${code}?mode=${mode}`);alertDailyCache.set(key,{value,time:Date.now()});return value;}
async function saveAlertEditor(){const raw=$('alertCost').value.trim(),cost=raw===''?null:Number(raw);if(cost!==null&&(!Number.isFinite(cost)||cost<=0)){$('alertEditorError').textContent='成本价须大于0，也可以留空。';return;}
 const editing=alertEditing;if(!editing||editing.mode!==state.mode)return;const button=$('saveAlertCost');button.disabled=true;
 try{const items=watchItems(),item=items.find(s=>s.code===editing.code);if(!item)return;const old=alertConfig(item);let holdingPlan=old.plan;
  const today=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date());const expired=holdingPlan?.date&&(Date.parse(today)-Date.parse(holdingPlan.date)>7*86400000);if(cost!==old.cost||cost&&expired){holdingPlan=null;if(cost){try{const daily=await getAlertDaily(item.code,editing.mode);holdingPlan={...daily.plan,support:daily.support};}catch{ /* Save optional cost even when the data source fails. */ }}}
  if(alertEditing!==editing||state.mode!==editing.mode||!$('alertEditor').open)return;
  // Read again after requests so a concurrent name update cannot be overwritten.
  const current=watchItems();const selected=current.find(s=>s.code===editing.code);if(!selected)return;selected.cost=cost;selected.holdingPlan=holdingPlan;delete selected.alerts;
  saveWatch(current);$('alertEditor').close();toast(cost?'已保存成本，自动按持仓状态解读':'已设为未持仓观察');realtimeLoop?.run('alerts',{force:true});
 }finally{button.disabled=false;}}
function renderAlertHistory(){const rows=alertHistory();$('alertHistoryList').innerHTML=rows.length?rows.map(r=>`<article class="alert-history-row"><b>${esc(r.name)} · ${esc(r.label)}</b><p>${esc(r.code)} · ${esc(r.role)} · 最新可用价 ${fmt(r.price)}${r.level?' · 参考 '+fmt(r.level):''}${r.cost?' · 成本 '+fmt(r.cost):''}</p><p>${esc(r.reason)}</p><small>行情 ${esc(r.asOf)} · 提醒 ${esc(r.notifiedAt)}${r.planDate?' · 日线参考 '+esc(r.planDate):''}${r.demo?' · 演示':''}</small></article>`).join(''):'<div class="empty compact">暂无触发记录<small>符合条件才提醒，数据不足时不生成操作判断。</small></div>';}
function showPriceNotices(rows){alertNotice=[...rows,...alertNotice].slice(0,8);$('priceNotice').hidden=false;$('priceNoticeList').innerHTML=alertNotice.map(r=>`<div><b>${esc(r.name)} · ${esc(r.label)}</b><p>${esc(r.role)} · 最新可用价 ${fmt(r.price)}</p><p>${esc(r.reason)}</p><small>${esc(r.asOf)}${r.demo?' · 演示':''}</small></div>`).join('');}
async function pollWatchAlerts(){
 const mode=state.mode,gen=state.generation,codes=watchItems().map(s=>s.code);if(!codes.length)return;
 let failures=0,checked=0,stale=0;
 if(!alertRoleMarket||alertRoleMarket.mode!==mode||Date.now()-alertRoleMarket.time>60000){try{const market=await api(`/api/terminal/market?mode=${mode}`);if(mode!==state.mode||gen!==state.generation)return;alertRoleMarket={mode,value:market,time:Date.now()};}catch{alertRoleMarket=null;}}
 for(let i=0;i<codes.length;i+=4){await Promise.all(codes.slice(i,i+4).map(async code=>{try{
  const [minuteResult,dailyResult]=await Promise.allSettled([api(`/api/terminal/intraday/${code}?mode=${mode}`),getAlertDaily(code,mode)]);if(mode!==state.mode||gen!==state.generation)return;
  if(minuteResult.status!=='fulfilled')throw Error();const minute=minuteResult.value,daily=dailyResult.status==='fulfilled'?dailyResult.value:null;
  const point=minute.points?.at(-1),quote={price:point?.price,asOf:minute.as_of};alertQuotes.set(mode+':'+code,quote);checked++;
  if(!PriceAlerts.fresh(quote,Date.now(),mode==='demo')){stale++;return;}if(!daily){failures++;return;}
  const items=watchItems(),item=items.find(s=>s.code===code);if(!item)return;const config=alertConfig(item);
  if(config.cost&&!item.holdingPlan){item.holdingPlan={...daily.plan,support:daily.support};persist(`sy-watch-${mode}`,items);config.plan=item.holdingPlan;}
  const group=(!alertRoleMarket?.value.stale&&!alertRoleMarket?.value.loading?alertRoleMarket?.value.groups:[])?.find(g=>g.leaders.some(s=>s.code===code));const role=group?.leaders.find(s=>s.code===code)?.role;
  const plan=config.cost?config.plan:daily.plan;
  const events=PriceAlerts.evaluate({cost:config.cost,plan,support:config.cost?config.plan?.support:daily.support,minute,funds:daily.funds,role,boardFunds:group?.funds,space:evaluatePlan(plan||{},minute)},Date.now(),mode==='demo');
  const seenKey=`sy-alert-seen-${mode}`,seenRaw=stored(seenKey,{}),seen=seenRaw&&typeof seenRaw==='object'&&!Array.isArray(seenRaw)?seenRaw:{},basis=config.cost||'watch';const rows=events.map(event=>({...event,key:`${minute.date}:${code}:${basis}:${event.kind}`,code,name:daily.name||item.name||code,notifiedAt:new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',dateStyle:'short',timeStyle:'medium'}).format(new Date()),demo:mode==='demo'})).filter(row=>!seen[row.key]);
  if(rows.length){for(const row of rows)seen[row.key]=row.asOf.slice(0,10);for(const key of Object.keys(seen)){if(Date.parse(minute.date)-Date.parse(seen[key])>14*86400000)delete seen[key];}persist(seenKey,seen);persist(`sy-alert-history-${mode}`,[...rows,...alertHistory()].slice(0,100));showPriceNotices(rows);}
 }catch{failures++;}}));if(mode!==state.mode||gen!==state.generation)return;}
 renderWatch();if($('alertHistoryDialog').open)renderAlertHistory();$('alertMonitorStatus').textContent=`本轮核对 ${checked} 只${failures?` · ${failures}只数据不完整`:''}${stale?` · ${stale}只行情过期，未判断`:''} · 仅核对最新可用分钟收盘价`;
}
function initWatchAlerts(){
 $('refresh').addEventListener('click',()=>realtimeLoop?.run('alerts',{force:true}));
 $('alertEditor').addEventListener('submit',e=>{e.preventDefault();saveAlertEditor();});$('closeAlertEditor').onclick=()=>$('alertEditor').close();
 $('openAlertHistory').onclick=()=>{renderAlertHistory();$('alertHistoryDialog').showModal();};$('closeAlertHistory').onclick=()=>$('alertHistoryDialog').close();$('dismissPriceNotice').onclick=()=>{alertNotice=[];$('priceNotice').hidden=true;};
}
