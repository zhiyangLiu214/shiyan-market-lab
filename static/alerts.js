(function(root){
 'use strict';
 const positive=v=>typeof v==='number'&&Number.isFinite(v)&&v>0;
 function fresh(quote,now=Date.now(),demo=false){
  if(!positive(quote?.price)||!/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(quote?.asOf||''))return false;
  const stamp=Date.parse(quote.asOf.replace(' ','T')+':00+08:00');if(!Number.isFinite(stamp)||stamp>now+60000)return false;
  if(demo)return true;
  const date=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date(now));if(quote.asOf.slice(0,10)!==date)return false;
  const local=new Date(now+8*3600000),minute=local.getUTCHours()*60+local.getUTCMinutes(),qm=Number(quote.asOf.slice(11,13))*60+Number(quote.asOf.slice(14,16));
  if(minute>=690&&minute<780)return qm>=689&&qm<=690;
  if(minute>=900)return qm>=899&&qm<=900;
  return now-stamp<=5*60000;
 }
 function evaluate({cost,plan,support,minute,funds,role,boardFunds,space},now=Date.now(),demo=false){
  const point=minute?.points?.at(-1),quote={price:point?.price,asOf:minute?.as_of};
  if(!fresh(quote,now,demo))return [];
  const price=quote.price,holding=positive(cost),events=[],day=minute.date;
  const add=(kind,label,reason,level=null)=>events.push({kind,label,reason,level,price,asOf:quote.asOf,role:role||'自选观察',cost:holding?cost:null,planDate:plan?.date||null});
  const validPlan=plan?.date&&plan.date<=day&&(Date.parse(day)-Date.parse(plan.date))<=7*86400000;
  if(validPlan&&positive(plan.stop)&&price<=plan.stop){
   if(holding)add(price<cost?'stop':'protect',price<cost?'止损关注':'止盈保护',`最新分钟收盘已低于技术失效参考 ${plan.stop}；${price<cost?'当前价低于记录成本':'当前仍高于记录成本，关注利润保护'}。`,plan.stop);
   else add('invalid','观察条件失效',`最新分钟收盘已低于日线失效参考 ${plan.stop}，暂停试仓观察。`,plan.stop);
  }else if(validPlan&&positive(plan.target1)&&price>=plan.target1){
   add(holding&&price>cost?'profit':'pressure',holding&&price>cost?'止盈关注':'到达压力位',`最新分钟收盘达到或越过首压力 ${plan.target1}${holding&&price>cost?'，相对记录成本已有浮盈':''}。`,plan.target1);
  }
  if(validPlan&&positive(support)&&price<=support&&!events.length)add(holding?'reduce':'support',holding?'减仓观察':'跌至支撑位',`最新分钟收盘已触及或跌破日线支撑 ${support}，观察支撑是否失效。`,support);
  const aligned=funds?.status==='ok'&&funds.date===day,boardAligned=boardFunds?.status==='ok'&&boardFunds.date===day;
  const tail=minute.points.slice(-2),weak=tail.length===2&&tail.every(p=>positive(p.average)&&p.price<p.average);
  if(holding&&!events.length&&weak&&aligned&&funds.net<0)add('reduce','减仓观察','连续两根分钟收盘低于分时均价，同时当日主力净额为负，价格与资金同步转弱。');
  const lastTime=point.time,minutes=t=>Number(t.slice(0,2))*60+Number(t.slice(3,5));
  const signal=(minute.signals||[]).filter(s=>['弱转强','均价附近承接'].includes(s.kind)&&minutes(lastTime)-minutes(s.time)>=0&&minutes(lastTime)-minutes(s.time)<=2).at(-1);
  const clock=new Date(now+8*3600000),clockMinute=clock.getUTCHours()*60+clock.getUTCMinutes();
  const activeSession=demo||(clock.getUTCDay()>0&&clock.getUTCDay()<6&&((clockMinute>=571&&clockMinute<=690)||(clockMinute>=781&&clockMinute<=900)));
  const good=activeSession&&aligned&&funds.net>0&&funds.ratio>0&&boardAligned&&boardFunds.net>0&&boardFunds.ratio>0&&!!role;
  if(!events.length&&validPlan&&good&&signal&&positive(point.average)&&price>=point.average&&Number.isFinite(space?.rr)&&space.rr>=1.5){
   if(!holding)add('trial','试仓观察',`${role}：最近两分钟内出现“${signal.kind}”，个股与板块同日主力资金为正，现价剩余盈亏比 ${space.rr}:1。`);
   else if(price>cost)add('add','加仓观察',`${role}：当前高于记录成本，最近两分钟内出现“${signal.kind}”，个股与板块资金同向，剩余盈亏比 ${space.rr}:1；需再核对持仓与可用仓位。`);
  }
  return events;
 }
 root.PriceAlerts={fresh,evaluate};if(typeof module!=='undefined'&&module.exports)module.exports=root.PriceAlerts;
})(typeof window!=='undefined'?window:globalThis);
