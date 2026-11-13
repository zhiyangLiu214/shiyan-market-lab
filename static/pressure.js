'use strict';
function pressureTargetText(plan,index){
 const level=plan.levels?.[index];
 return level?`${fmt(level.price)}${level.high>level.price?'–'+fmt(level.high):''}`:'暂无明确压力位';
}
function pressureHTML(plan,name='',minute=null){
 return `<section class="panel pressure-panel"><div class="panel-title"><h2>压力位目标 · ${esc(plan.period)}</h2><span class="badge">${esc(plan.assessment)}</span></div>
 <div class="pressure-context">${esc(name)} · ${esc(plan.date)} ${esc(plan.as_of||'')} · 参考 ${fmt(plan.reference)} / 止损 ${fmt(plan.stop)}</div>
 <div class="pressure-grid">${[0,1].map(i=>{const level=plan.levels?.[i];return `<div><small>${i===0?'第一压力区域':'第二压力区域'}</small><strong class="num">${pressureTargetText(plan,i)}</strong><p>${level?`${esc(level.basis)}<br>代表高点：${esc(level.date)}`:'当前可用历史中未找到对应的上方压力位'}</p></div>`;}).join('')}</div>
 <div class="pressure-risk"><div><small>计划首压力盈亏比</small><strong class="num ${plan.rr===null?'muted':plan.rr<1.5?'risk-low':'risk-fair'}">${plan.rr===null?'—':fmt(plan.rr)+':1'}</strong></div><div><small>从计划参考价到首压力</small><strong class="num">${pct(plan.upside)}</strong></div><div><small>计划第二压力盈亏比</small><strong class="num">${plan.rr2===null?'—':fmt(plan.rr2)+':1'}</strong></div></div>
 ${typeof remainingPlanHTML==='function'?remainingPlanHTML(plan,minute):''}
 <p class="chart-caption">盈亏比 =（压力区域下沿 − 参考价）/（参考价 − 止损）。${esc(plan.target_basis)}。${plan.period==='分钟线'?(plan.as_of?'压力依据固定在首信号 '+esc(plan.as_of)+' 之前，不用之后的分钟高点反推。':'尚未触发信号，不生成分钟压力与盈亏比。'):'排除末根日线，以已确认的历史高点识别压力；不复权数据遇除权需复核。'} 这是价格结构参考，未提供到达概率。</p>
 <details class="projection"><summary>盈亏比推算价（非压力位）</summary><p>2R：${fmt(plan.rr_target1)}；3R：${fmt(plan.rr_target2)}。R = 参考价 − 止损价。仅作独立推算，不用于替代缺失的压力位。</p></details></section>`;
}
