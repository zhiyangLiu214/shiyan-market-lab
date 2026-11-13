'use strict';
let backtestId = null;
let backtestTimer = null;
let backtestLoading = false;

async function loadBacktests() {
    if (backtestLoading) return;
    backtestLoading = true;
    try {
        if (!$('backtestEnd').value) $('backtestEnd').value = new Date().toLocaleDateString('en-CA', {timeZone:'Asia/Shanghai'});
        const [list, strategies] = await Promise.all([
            api('/api/terminal/backtests'),
            api('/api/terminal/strategy-sim?mode=live')
        ]);
        const checked = [...$('backtestStrategies').querySelectorAll('input:checked')].map(el => el.value);
        $('backtestStrategies').innerHTML = '<legend>选择策略（最多3套）</legend>' +
            strategies.items.map((item,index) => `<label><input type="checkbox" value="${esc(item.id)}" ${checked.length ? (checked.includes(item.id)?'checked':'') : (index<3?'checked':'')}>${esc(item.config.name)}</label>`).join('');
        $('backtestHistory').innerHTML = list.items.map(item => `<option value="${esc(item.id)}">${esc(item.start)} 至 ${esc(item.end)} · ${esc(item.created.slice(0,16))}</option>`).join('');
        if (!list.items.some(item => item.id === backtestId)) backtestId = list.items[0]?.id || null;
        if (backtestId) {
            $('backtestHistory').value = backtestId;
            await readBacktest();
        } else $('backtestResult').innerHTML = '<div class="panel backtest-notice">尚无历史回溯记录。选择策略和日期后开始。</div>';
    } catch (error) {
        $('backtestError').textContent = error.message;
    } finally {
        backtestLoading = false;
    }
}

function backtestResultHTML(report) {
    const partial = report.status === 'partial';
    const detail = (report.assumptions || []).map(text => `<p>${esc(text)}</p>`).join('');
    const cards = (report.results || []).map(result => {
        const trades = result.trades || [];
        return `<article class="panel backtest-card"><h2>${esc(result.name)}</h2>
            <small>${result.complete ? (partial ? '已覆盖样本收益' : '扣费后收益率') : '收益尚未核算完整'}</small>
            <div class="backtest-return ${sign(result.return_pct)}">${pct(result.return_pct)}</div>
            <div class="sim-metrics">${[
                ['期末权益',fmt(result.nav)],['收盘最大回撤',fmt(result.drawdown)+'%'],
                ['已实现盈亏',fmt(result.realized)],['累计费用',fmt(result.fees)],
                ['买入笔数',trades.filter(t=>t.side==='buy').length],['卖出笔数',trades.filter(t=>t.side==='sell').length]
            ].map(([label,value])=>`<div><small>${label}</small><b class="num">${value}</b></div>`).join('')}</div>
            ${paperSpark(result.equity.map(row=>({at:row.date,nav:row.nav})))}
            ${result.errors.map(row=>`<p class="down">${esc(row.date)} ${esc(row.code)} ${esc(row.reason)}</p>`).join('')}
            <details><summary>期末持仓 · ${result.positions.length}只</summary>${result.positions.map(p=>`<p>${esc(p.name)} ${esc(p.code)} · ${p.qty}股 · 市值${fmt(p.value)}</p>`).join('')||'<p>空仓</p>'}</details>
            <details><summary>交易明细 · ${trades.length}笔</summary><div class="backtest-scroll">${trades.map(t=>`<div class="backtest-trade"><b>${esc(t.date)} ${esc(t.time)} · ${t.side==='buy'?'买入':'卖出'} ${esc(t.name)}</b><p>${esc(t.code)} · ${t.qty}股 × ${fmt(t.price)} · 费用${fmt(t.fees)}${t.pnl==null?'':' · 盈亏'+fmt(t.pnl)}</p><p>${esc(t.reason)}</p></div>`).join('')}</div></details>
            <details><summary>每日14:30入选 · ${result.selections.length}条</summary><div class="backtest-scroll">${result.selections.map(row=>`<p>${esc(row.date)} · ${esc(row.name)} ${esc(row.code)} · 排名分${fmt(row.rank,1)}</p>`).join('')}</div></details>
            <details><summary>入选后未买入 · ${result.skipped.length}条</summary><div class="backtest-scroll">${result.skipped.map(row=>`<p>${esc(row.date)} ${esc(row.name)} · ${esc(row.reason)}</p>`).join('')}</div></details>
        </article>`;
    }).join('');
    return `<div class="panel backtest-notice"><b>${esc(report.start)} 至 ${esc(report.end)} · ${esc(report.progress)}</b>
        <p>14:30选股 · 收盘价买入 · 每套初始100,000元 · ${report.sessions?.length||0}个交易日${report.coverage_pct==null?'':' · 数据覆盖'+fmt(report.coverage_pct)+'%'}</p>
        ${partial?'<p class="down">存在数据缺口。当前收益仅代表已覆盖的样本，需补齐数据后复核。</p>':''}
        <details><summary>本次规则与数据范围</summary><p>${esc(report.universe_note||'')} · ${report.universe_count||0}只股票</p>${detail}</details>
        ${report.errors?.length?`<details><summary>查看数据缺口 · ${report.errors.length}条</summary><div class="backtest-scroll">${report.errors.map(row=>`<p>${esc(row.date||'全区间')} · ${esc(row.code)} · ${esc(row.reason)}</p>`).join('')}</div></details>`:''}
        </div><div class="backtest-columns">${cards}</div>`;
}

async function readBacktest() {
    clearTimeout(backtestTimer);
    if (!backtestId) return;
    const requested = backtestId;
    try {
        const report = await api('/api/terminal/backtests?id='+encodeURIComponent(requested));
        if (requested !== backtestId) return;
        const open = [...$('backtestResult').querySelectorAll('details')].map(el=>el.open);
        $('backtestResult').innerHTML = backtestResultHTML(report);
        $('backtestResult').querySelectorAll('details').forEach((el,index)=>el.open=!!open[index]);
        $('backtestRun').disabled = report.status==='running';
        if (report.status==='running' && state.page==='backtest') backtestTimer=setTimeout(readBacktest,4000);
    } catch(error) {$('backtestError').textContent=error.message;}
}

document.addEventListener('DOMContentLoaded',()=>{
    $('backtestForm').onsubmit=async event=>{
        event.preventDefault();
        $('backtestError').textContent='';
        $('backtestRun').disabled=true;
        try {
            const ids=[...$('backtestStrategies').querySelectorAll('input:checked')].map(el=>el.value);
            const report=await api('/api/terminal/backtests',{method:'POST',body:JSON.stringify({ids,start:$('backtestStart').value,end:$('backtestEnd').value})});
            backtestId=report.id;
            await loadBacktests();
        } catch(error) {$('backtestError').textContent=error.message;$('backtestRun').disabled=false;}
    };
    $('backtestRefresh').onclick=loadBacktests;
    $('backtestHistory').onchange=()=>{backtestId=$('backtestHistory').value;readBacktest();};
});
