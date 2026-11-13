'use strict';
let sourcesBusy=false;
async function loadSources(){
 if(sourcesBusy||document.activeElement?.closest('#sourceControls select'))return;
 sourcesBusy=true;
 try{
  const s=await api('/api/terminal/sources');
  $('quoteSource').value=s.preferred;$('fundSource').value=s.funds_preferred||'auto';
  const tushare=(s.connections||[]).find(c=>c.provider==='tushare');
  const tushareOption=$('quoteSource').querySelector('option[value="tushare"]');
  if(tushareOption)tushareOption.disabled=!tushare?.configured;
  const rows=s.providers||[],labels={daily:'不复权日线',daily_qfq:'前复权日线',minute:'分钟',funds:'板块资金'};
  $('sourceHealth').innerHTML=rows.length?rows.map(p=>`<span>${esc(labels[p.channel]||p.channel)} · ${esc(p.name)}：${esc({ok:'可用',observation:'仅供观察',unavailable:'不可用'}[p.status]||p.status)} · ${esc(p.reason)} <small>${esc(p.at)}</small></span>`).join('<br>'):'尚未请求数据；实际来源将在个股、分时和板块卡片内显示。';
  for(const p of s.stock_funds_health||[]){
   const retry=p.retry_in>0?`；${p.retry_in}秒后允许自动复检`:'；下次资金请求自动复检';
   const outage=p.outage_since?`；本次故障始于 ${p.outage_since}`:'';
   const recovered=p.recovered_at?`；最近恢复 ${p.recovered_at}`:'';
   $('sourceHealth').innerHTML+=`<br><span>资金通道 · ${esc(p.host)}：${esc(p.status==='ok'?'最近响应成功':'不可用'+retry)} · ${esc(p.reason)}${esc(outage+recovered)} <small>最近成功 ${esc(p.last_success||'尚无成功记录')}；累计失败 ${esc(p.failures_total||0)} 次</small></span>`;
  }
  for(const c of s.connections||[]){
   $('sourceHealth').innerHTML+=`<br><span>${esc(c.name)}接入：${esc(c.configured?'已配置，权限按项核验':'未配置')} · ${esc(c.reason)}</span>`;
   for(const check of c.checks||[])$('sourceHealth').innerHTML+=`<br><small>${esc(check.api)} · ${esc(check.status==='ok'?'最近响应成功':'未通过')} · ${esc(check.reason)} · ${esc(check.at)}</small>`;
  }
 }catch(e){$('sourceHealth').textContent='来源状态暂不可用：'+e.message;}
 finally{sourcesBusy=false;}
}
async function saveSources(){
 $('saveSources').disabled=true;
 try{
  await api('/api/terminal/sources',{method:'POST',body:JSON.stringify({preferred:$('quoteSource').value,funds_preferred:$('fundSource').value})});
  state.details.clear();toast('来源优先级已保存；该源不可用时自动回退');await loadSources();
  if(['main','strategy'].includes(state.page))await refresh({background:true});
  if(state.page==='watch'&&state.selected.watch)await showDetail(state.selected.watch,'watch',{background:true,force:true});
 }catch(e){toast(e.message);}finally{$('saveSources').disabled=false;}
}
document.addEventListener('DOMContentLoaded',()=>{
 $('saveSources').onclick=saveSources;
 $('sourceControls').addEventListener('toggle',()=>{if($('sourceControls').open)loadSources();});
});

setInterval(()=>{if(!document.hidden&&$('sourceControls')?.open)loadSources();},10000);
