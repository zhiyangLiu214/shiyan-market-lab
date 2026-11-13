'use strict';
// Evaluate saved levels using minute closes after the plan's own timestamp.
function evaluatePlan(plan, minute){
 const out={status:'等待分钟行情核对',current:null,rr:null,space:null,risk:null,asOf:null,event:null};
 if(!minute?.points?.length)return out;
 const latest=minute.points.at(-1);out.current=latest.price;out.asOf=minute.as_of||`${minute.date} ${latest.time}`;
 if(plan.reference==null)return {...out,status:'未触发分钟计划'};
 if(!plan.date||minute.date<plan.date)return {...out,status:'分钟数据早于计划，无法核对'};
 const points=minute.points.filter(p=>minute.date>plan.date||!plan.as_of||p.time>plan.as_of);
 for(const p of points){
  if(Number.isFinite(plan.stop)&&p.price<=plan.stop){out.event=`${minute.date} ${p.time}`;out.status='分钟收盘已触及止损参考';break;}
  if(Number.isFinite(plan.target1)&&p.price>=plan.target1){out.event=`${minute.date} ${p.time}`;out.status='分钟收盘已触及首压力';break;}
 }
 if(!out.event)out.status=minute.date>plan.date?'跨日计划 · 仅核对当日分钟':'观察中 · 尚无分钟收盘触及';
 if(Number.isFinite(plan.stop)&&latest.price<=plan.stop)out.status='当前价已低于止损参考';
 else if(Number.isFinite(plan.target1)&&latest.price>=plan.target1)out.status='当前价已越过首压力';
 if(!out.event&&Number.isFinite(plan.stop)&&latest.price>plan.stop&&Number.isFinite(plan.target1)&&latest.price<plan.target1){
  out.rr=+( (plan.target1-latest.price)/(latest.price-plan.stop)).toFixed(2);
  out.space=+( (plan.target1/latest.price-1)*100).toFixed(2);
  out.risk=+( (1-plan.stop/latest.price)*100).toFixed(2);
 }
 return out;
}
function remainingPlanHTML(plan,minute){
 const e=evaluatePlan(plan,minute);
 return `<div class="current-plan"><div class="guide-head"><h3>现价空间与计划状态</h3><span class="badge">${esc(e.status)}</span></div><div class="pressure-risk"><div><small>最新分钟价</small><strong class="num">${fmt(e.current)}</strong></div><div><small>现价剩余盈亏比</small><strong class="num ${e.rr!=null&&e.rr<1.5?'risk-low':''}">${e.rr==null?'—':fmt(e.rr)+':1'}</strong></div><div><small>到首压力 / 到止损</small><strong class="num">${e.space==null?'—':fmt(e.space)+'% / '+fmt(e.risk)+'%'}</strong></div></div><p class="chart-caption">${e.asOf?'分钟截至 '+esc(e.asOf)+'。':'分钟数据暂不可用，日线收盘不作为最新报价。'}${e.event?' 本次可用分钟内首次触及：'+esc(e.event)+'。':''} ${e.rr==null?'未触发计划、缺目标或已越过目标／止损时不展示入场盈亏比。':'剩余盈亏比 =（首压力 − 最新分钟价）/（最新分钟价 − 止损）。'}仅按可用分钟收盘核对，未覆盖分钟内高低点及跨日缺失路径；不代表实际成交或完整历史状态。不复权数据遇除权须重新核对。</p></div>`;
}
function dataBasisHTML(stock){
 const m=stock.minute,f=stock.funds,last=m?.points?.at(-1);
 return `<section class="panel data-basis"><div class="panel-title"><h2>数据时间与口径</h2><span class="badge">日线结构 + 分钟执行</span></div><div class="basis-grid"><div><small>日线 / 计划基准</small><b>${esc(stock.date)} · 不复权</b><p>收盘 ${fmt(stock.price)} · ${pct(stock.pct)}；因子评分据此计算。</p></div><div><small>分钟行情</small><b>${last?esc(m.as_of||m.date+' '+last.time):'暂不可用'}</b><p>${last?'最新可用价 '+fmt(last.price):esc(stock.minute_error||'等待分钟数据')} · 页面轮询不等于逐笔行情。</p></div><div><small>主力资金</small><b>${esc(f?.date||'未知')} · 交易日累计</b><p>获取 ${esc(f?.retrieved_at||'未知')}；资金快照不等于每分钟增量。</p></div></div></section>`;
}
function openingHTML(data){
 const o=data.opening;if(!o)return '';
 return `<section class="panel opening-panel"><div class="panel-title"><h2>开盘量能观察</h2><span class="badge">非集合竞价</span></div><p>${o.available?`${esc(o.start)}–${esc(o.end)} · 前5分钟成交量 <b>${fmt(o.volume,0)} 手</b> · 成交额 <b>${money(o.amount)}</b>`:'开盘分钟数据不完整，暂不计算。'}</p><p class="chart-caption">由开盘后的分钟行情合计；没有09:25竞价成交量、未匹配量和委托队列，不生成竞价抢筹评分。主力资金另看独立资金面板。</p></section>`;
}
