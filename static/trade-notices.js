'use strict';
let tradeNoticeMode=null,tradeNoticeState={cursors:{},queue:[]},tradeNoticeBusy=false,tradeNoticeError=false;
function tradeNoticeKey(mode){return 'sy-operation-notices-v1-'+mode;}
function readTradeNoticeState(mode){const saved=stored(tradeNoticeKey(mode),{}),s=saved&&typeof saved==='object'?saved:{},raw=s.cursors&&typeof s.cursors==='object'&&!Array.isArray(s.cursors)?s.cursors:{};return {cursors:Object.fromEntries(Object.entries(raw).filter(([,v])=>Number.isSafeInteger(v)&&v>=0)),queue:Array.isArray(s.queue)?s.queue.filter(n=>n&&typeof n.id==='string'):[]};}
function saveTradeNoticeState(){try{localStorage.setItem(tradeNoticeKey(tradeNoticeMode),JSON.stringify(tradeNoticeState));}catch{if(!tradeNoticeError)toast('提醒暂未保存到浏览器，刷新后可能再次显示；操作记录仍保留在网站。');tradeNoticeError=true;}}
function resetTradeNotices(){tradeNoticeMode=state.mode;tradeNoticeState=readTradeNoticeState(state.mode);tradeNoticeError=false;renderTradeNotices();}
function renderTradeNotices(){const q=tradeNoticeState.queue;$('simNotice').hidden=!q.length;$('simNoticeTitle').textContent='模拟操作提醒 · 待读 '+q.length;$('simNoticeClose').textContent=q.length>3?'已读，下一组':'已读并关闭';$('simNoticeItems').innerHTML=q.slice(0,3).map(n=>`<article class="operation-notice"><b>${esc(n.title||'模拟提醒')}</b><small>${esc(n.origin||'')} · ${n.source==='manual'?'手动模拟':n.source==='auto'?'自动模拟':n.phase==='signal'?'持仓信号':'模拟账户'}</small><p>${esc(n.name||'')}${n.code?' · '+esc(n.code):''}</p><p>${esc(n.text||'')}</p><small>${esc(n.at||'')}</small></article>`).join('');}
function acknowledgeTradeNotices(all=false){if(tradeNoticeMode!==state.mode)resetTradeNotices();tradeNoticeState.queue.splice(0,all?tradeNoticeState.queue.length:3);saveTradeNoticeState();renderTradeNotices();}
async function pollTradeNotices(){
 if(tradeNoticeBusy)return;if(tradeNoticeMode!==state.mode)resetTradeNotices();
 tradeNoticeBusy=true;const mode=state.mode,gen=state.generation;
 try{
  for(let batch=0;batch<5;batch++){
   const result=await api('/api/terminal/trade-notices?mode='+mode+'&cursors='+encodeURIComponent(JSON.stringify(tradeNoticeState.cursors)));
   if(mode!==state.mode||gen!==state.generation)return;
   const ids=new Set(tradeNoticeState.queue.map(n=>n.id));
   const oldSignals=new Set(stored('sy-sim-seen-'+mode,[]));
   for(const n of result.events||[])if(!ids.has(n.id)&&n.seq>(tradeNoticeState.cursors[n.stream]||0)&&!(n.stream==='signals'&&oldSignals.has(n.id.replace(/^signals:/,'')))){tradeNoticeState.queue.push(n);ids.add(n.id);}
   for(const [stream,seq] of Object.entries(result.cursors||{}))tradeNoticeState.cursors[stream]=Math.max(tradeNoticeState.cursors[stream]||0,seq);
   saveTradeNoticeState();renderTradeNotices();if(!result.has_more)break;
  }
 }catch(e){if(mode===state.mode&&gen===state.generation&&!tradeNoticeError){toast('操作提醒暂未更新：'+e.message+'；保留未读，稍后重试。');tradeNoticeError=true;}}
 finally{tradeNoticeBusy=false;}
}
function initTradeNotices(){resetTradeNotices();$('simNoticeClose').onclick=()=>acknowledgeTradeNotices();$('simNoticeAllRead').onclick=()=>acknowledgeTradeNotices(true);window.addEventListener('storage',e=>{if(e.key===tradeNoticeKey(state.mode))resetTradeNotices();});}
