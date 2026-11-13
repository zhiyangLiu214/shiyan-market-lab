'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (n, digits=2) => n === null || n === undefined || !Number.isFinite(Number(n)) ? '—' : Number(n).toFixed(digits);
const pct = n => n === null || n === undefined ? '—' : `${Number(n)>=0?'+':''}${fmt(n)}%`;
const sign = n => Number(n) >= 0 ? 'up' : 'down';

function money(value){if(value===null||value===undefined||!Number.isFinite(Number(value)))return '—';const v=Number(value),a=Math.abs(v);return `${v>0?'+':v<0?'−':''}${fmt(a/(a>=1e8?1e8:1e4))}${a>=1e8?' 亿':' 万'}`;}
function fundsSummary(f){if(!f||f.net===null||f.net===undefined)return `<span class="fund-missing">主力净流入 — · ${esc(f?.reason||'待获取资金数据')}</span>`;return `<span class="fund-value ${sign(f.net)}">主力净流入 <b class="num">${money(f.net)}</b></span><small>${esc(f.date)}${f.as_of?' '+esc(f.as_of):''} · ${f.status==='stale'?'旧快照，更新失败':esc(f.period||'交易日累计')}</small>`;}
function fundsEvidence(stock){const f=stock.funds,date=stock.quote_date||stock.date;if(!f||f.status!=='ok'||f.net===null)return '资金数据未确认，不作为当前信号依据。';if(!date||f.date!==date)return `资金截至 ${f.date}，与价格日期未对齐，仅供历史观察。`;return `${f.date} 主力${f.net>0?'净流入':f.net<0?'净流出':'净额为零'} ${money(f.net)}${f.ratio===null?'':`，占比 ${pct(f.ratio)}`}。${f.net<0?'资金与价格可能背离，等待进一步确认。':f.positive_days>=2?`最近 ${f.positive_days} 个交易日持续净流入，仍需量价确认。`:'单日资金不代表持续流入，继续观察。'}`;}
function fundsPanel(stock){const f=stock.funds;return `<section class="panel funds-panel"><div class="panel-title"><h2>主力资金 · 交易日累计</h2><span class="muted">${esc(f?.source||'东方财富资金流')}</span></div><div class="funds-grid"><div><small>主力净流入</small><strong class="num ${f?.net==null?'muted':sign(f.net)}">${money(f?.net)}</strong></div><div><small>主力净占比</small><strong class="num">${pct(f?.ratio)}</strong></div><div><small>大单净额</small><strong class="num">${money(f?.large)}</strong></div><div><small>超大单净额</small><strong class="num">${money(f?.super_large)}</strong></div><div><small>近 5 个交易日净额</small><strong class="num">${money(f?.five_day_net)}</strong></div><div><small>日内振幅</small><strong class="num">${fmt(stock.amplitude)}%</strong></div></div><p class="fund-explanation"><span class="muted">按当前页面设置轮询交易日累计快照；不是逐笔资金或每分钟增量，数据源可能延迟。</span><br>${esc(fundsEvidence(stock))}${f?.history_source?`<br><span class="muted">历史资金来源：${esc(f.history_source)} · ${esc(f.history_period||"已完成交易日历史")}</span>`:""}</p>${f?.history?.length?`<div class="fund-history">${f.history.map(row=>`<div><small>${esc(row.date.slice(5))}</small><b class="num ${sign(row.net)}">${money(row.net)}</b><i style="height:${Math.max(2,Math.abs(row.net)/Math.max(...f.history.map(r=>Math.abs(r.net)),1)*38)}px;background:${row.net>=0?'var(--red)':'var(--green)'}"></i></div>`).join('')}</div>`:''}<div class="chart-caption">${f?.date?`交易日 ${esc(f.date)} · 获取 ${esc(f.retrieved_at)} · `:''}${esc(f?.status==='stale'?f.reason:f?.net==null?(f?.reason||'资金源暂不可用'):'主力净额采用资金源的大单与超大单口径；成交额与资金净额分别展示。')} 振幅 =（当日最高 − 最低）/ 昨收。资金未计入三维分数。</div></section>`;}

const factorNames = ['结构突破','趋势强度','量价动量'];
const pageInfo = {sim:['CUSTOM STRATEGY','执行自己的<span>选股与退出规则</span>','固定时间买入，分时观察退出；每套策略独立记录持仓与盈亏。'],notes:['YEREN NOTEBOOK','把干货沉淀为<span>操作笔记</span>','记录依据、适用条件和操作关键，回到原视频核对。'],paper:['PAPER TRADING','双账户<span>模拟交易</span>','独立筛选池、统一成交与风控，比较前向模拟盈亏。'],main:['MARKET OVERVIEW','把握市场的<span>主线脉络</span>','先判断主线与阶段，再观察龙头、趋势龙与补涨角色。'],strategy:['STRATEGY INSIGHTS','读懂强势背后的<span>量价关系</span>','用透明规则解释板块状态，把判断依据放在结论之前。'],watch:['PERSONAL WATCHLIST','建立自己的<span>市场观察池</span>','跟踪价格、技术结构与历史信号，保存你的研究线索。'],scan:['MULTI-FACTOR RESEARCH','让强势筛选<span>有据可循</span>','板块涨幅与多股动量共振通过后，再进行三维加权排名。']};
function stored(key, fallback){try{return JSON.parse(localStorage.getItem(key))??fallback;}catch{return fallback;}}
const initialMode = new URLSearchParams(location.search).get('mode');
const state = {mode:initialMode==='demo'?'demo':initialMode==='live'?'live':stored('sy-mode','live'),page:'main',market:null,scan:[],weights:[40,30,30],generation:0,details:new Map(),selected:{watch:null,scan:null},boardChecks:[],detailTokens:{watch:0,scan:0},scanRunning:false};
let realtimeLoop;
if(!['live','demo'].includes(state.mode))state.mode='live';
function persist(key,value){try{localStorage.setItem(key,JSON.stringify(value));}catch{toast('浏览器存储不可用，本次修改无法持久保存');}}
function watchItems(){const raw=stored(`sy-watch-${state.mode}`,state.mode==='demo'?[{code:'000001',name:'示例·云算科技',board:'算力设备'},{code:'000005',name:'示例·精工智造',board:'高端制造'}]:[]);return Array.isArray(raw)?raw.filter(s=>s&&/^[034689]\d{5}$/.test(s.code)).slice(0,50):[];}
function saveWatch(items){persist(`sy-watch-${state.mode}`,items);renderWatch();}
function toast(message){$('toast').textContent=message;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').textContent='',3500);}
async function api(path, options={}){const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),45000);try{const response=await fetch(path,{...options,signal:controller.signal,headers:{'Content-Type':'application/json',...options.headers}});if(!response.headers.get('content-type')?.includes('application/json'))throw Error(response.status===401?'站点登录已失效，请刷新后重新登录':('服务暂时未就绪（HTTP '+response.status+'），请稍后重试'));const result=await response.json();if(!response.ok||!result.ok)throw Error(result.error||'请求失败');return result.data;}catch(error){if(error.name==='AbortError')throw Error('行情请求超时，请稍后重试');throw error;}finally{clearTimeout(timer);}}
function navigate(page){document.body.classList.toggle('notes-active',page==='notes');window.scrollTo({top:0,behavior:'auto'});state.page=page;document.querySelectorAll('.page').forEach(el=>el.hidden=el.id!==`${page}Page`);document.querySelectorAll('[data-page]').forEach(el=>el.classList.toggle('active',el.dataset.page===page));$('pageEyebrow').textContent=pageInfo[page][0];$('pageTitle').innerHTML=pageInfo[page][1];$('pageSubtitle').textContent=pageInfo[page][2];if(page==='sim')loadStrategySim();if(page==='notes')loadNotes();if(page==='paper')loadPaper();if(page==='strategy')renderStrategy();if(page==='watch')renderWatch();if(realtimeLoop)realtimeLoop.tick();}
function updateMode(){ $('mode').value=state.mode;$('demoBanner').hidden=state.mode!=='demo';$('modeLabel').textContent=state.mode==='demo'?'演示数据':'行情接口';$('statusDot').style.background=state.mode==='demo'?'var(--gold)':'var(--muted)'; }
async function changeMode(mode){history.replaceState(null,'',`?mode=${mode}`);state.mode=mode;paperState=null;$('paperContent').innerHTML='';if(state.page==='paper')loadPaper();alertNotice=[];$('priceNotice').hidden=true;$('alertEditor').close();$('alertHistoryDialog').close();$('alertMonitorStatus').textContent='等待下一次价格核对';clearStockSearch();persist('sy-mode',mode);state.generation++;resetStrategySim();state.marketBusy=null;if(realtimeLoop)realtimeLoop.reset();state.market=null;state.scan=[];state.boardChecks=[];$('boardChecks').innerHTML='';state.details.clear();state.selected={watch:null,scan:null};state.detailTokens.watch++;state.detailTokens.scan++;state.scanRunning=false;$('startScan').disabled=false;$('scanProgress').textContent='';$('scanList').innerHTML='<div class="empty compact">点击「开始扫描」生成候选榜单</div>';$('scanCount').textContent='尚未扫描';['watch','scan'].forEach(k=>$(k+'Detail').innerHTML='<div class="empty">选择股票，查看日线与因子分析</div>');updateMode();renderWatch();renderStrategy();await refresh();if(realtimeLoop)realtimeLoop.tick();}
async function refresh({background=false}={}){
 const gen=state.generation;if(state.marketBusy===gen)return;state.marketBusy=gen;
 $('refresh').disabled=true;
 if(!state.market)$('marketContent').innerHTML='<div class="empty loading">正在连接行业行情…</div>';
 try{
  const result=await api(`/api/terminal/market?mode=${state.mode}`);if(gen!==state.generation)return;
  if(result.loading){
   $('statusDot').style.background='var(--gold)';$('updated').textContent='正在获取主线数据';
   $('notice').textContent=result.last_error||'正在后台核验板块、资金及涨停池，完成后自动显示。';
   if(!state.market)$('marketContent').innerHTML='<div class="empty loading">正在获取真实主线行情…<small>数据源响应较慢，后台正在重试；尚未生成筛选结论。</small></div>';
   clearTimeout(state.marketLoadingTimer);state.marketLoadingTimer=setTimeout(()=>{if(gen===state.generation&&!document.hidden&&['main','strategy'].includes(state.page))refresh({background:true});},2000);
   return;
  }
  clearTimeout(state.marketLoadingTimer);state.market=result;renderMarket();renderStrategy();$('updated').textContent=`${result.stale?'旧快照':'获取'} ${result.updated_at}`;
  $('statusDot').style.background=state.mode==='demo'?'var(--gold)':result.stale?'var(--gold)':'var(--green)';$('notice').textContent=result.stale?`旧快照 · ${result.updated_at} · ${result.last_error||'更新暂时失败'}。仅供查看，不作为新买单依据。`:(result.errors||[]).join('；');
  if(result.stale&&result.refreshing){clearTimeout(state.marketLoadingTimer);state.marketLoadingTimer=setTimeout(()=>{if(gen===state.generation&&!document.hidden&&['main','strategy'].includes(state.page))refresh({background:true});},2000);}
 }catch(error){
  if(gen!==state.generation)return;$('statusDot').style.background='var(--red)';
  if(state.market){$('notice').textContent=`更新失败：${error.message}。保留 ${state.market.updated_at} 获取的结果，自动稍后重试。`;}
  else{$('updated').textContent='行情暂不可用';$('marketContent').innerHTML=`<div class="error-box"><h3>暂时无法连接行情源</h3><p>${esc(error.message)}</p><button class="button" data-action="retry">重新连接</button><button class="button gold" data-action="demo">体验演示样本 ↗</button></div>`;renderStrategy();}
 }finally{if(state.marketBusy===gen){state.marketBusy=null;$('refresh').disabled=false;}}
}
function renderMarket(){
 if(!state.market)return;
 if(!state.market.groups.length){$('marketContent').innerHTML='<div class="empty">当前无行业通过资金与扩散门槛<small>不自动退回纯涨幅排序；可查看更新时间与提示</small></div>'+mainlineRulesHTML();return;}
 const rulesOpen=!!document.querySelector('.mainline-rules[open]'),expandedScores=new Set([...document.querySelectorAll('.sector-card')].filter(card=>card.querySelector('.score-breakdown[open]')).map(card=>card.dataset.sectorCode));
 const scroll=window.scrollY;const retained=new Map([...document.querySelectorAll('[data-main-code]')].map(article=>[article.dataset.mainCode,article.querySelector('.intraday-body')]));
 const groups=state.market.groups;
 $('marketContent').innerHTML=`<div class="dashboard-toolbar"><span>${state.market.stale?'旧快照，仅供历史查看':groups.some(g=>g.board_type==='main')?'已有确认主线':'暂无确认主线'} · ${groups.length} 个板块 · 确认 / 资金趋势 / 潜在待涨</span>${mainlineRulesHTML()}</div><div class="mainline-board">
 ${groups.map(g=>`<section class="mainline-column">${sectorCard(g)}<div class="mainline-group"><div class="column-heading"><h2>板块内角色</h2><small>${g.leaders.length} 个候选</small></div>
 ${state.market.readonly?'<p class="muted">备用资金观察：尚未取得完整个股数据，暂不生成角色。</p>':mainRoleGroupsHTML(g)}<details class="column-coverage"><summary>数据覆盖与范围</summary><p>${esc(g.coverage)}</p></details></div></section>`).join('')}</div>
 <details class="dashboard-source"><summary>数据来源、扫描范围与限制 · ${esc(state.market.updated_at)}</summary><p>${esc(state.market.source)} · ${esc(state.market.scope)}。角色规则为本工具近似模型；涨停统计仅为涨停池覆盖口径。</p></details>`;
 if(rulesOpen)document.querySelector('.mainline-rules').open=true;for(const card of document.querySelectorAll('.sector-card'))if(expandedScores.has(card.dataset.sectorCode))card.querySelector('.score-breakdown').open=true;
 for(const article of document.querySelectorAll('[data-main-code]')){
  const old=retained.get(article.dataset.mainCode);if(!old)continue;
  article.querySelector('.intraday-body').replaceWith(old);
  const button=article.querySelector('.intraday-toggle');button.setAttribute('aria-expanded',String(!old.hidden));button.textContent=old.hidden?'展开分时与信号 ↓':'收起分时与信号 ↑';
  if(old._snapshot)article.querySelector('.main-operation').innerHTML=mainGuideHTML(stockMeta(article.dataset.mainCode),old._snapshot);
 }
 window.scrollTo({top:scroll,behavior:'auto'});
}
function stockMeta(code){const candidates=[...(state.market?.groups.flatMap(g=>g.leaders)||[]),...state.scan,...[...state.details.values()].reverse(),...watchItems()].filter(s=>s.code===code);return candidates.find(s=>s.name&&s.name!==code)||candidates[0]||{code,name:code,board:''};}
function syncWatchName(detail){if(!detail.name||detail.name===detail.code)return;const items=watchItems(),item=items.find(s=>s.code===detail.code);if(!item)return;const board=detail.board||item.board||'';if(item.name===detail.name&&item.board===board)return;item.name=detail.name;item.board=board;saveWatch(items);}
function renderStrategy(){
 if(!state.market?.groups.length){$('strategyContent').innerHTML='<div class="empty">暂无通过资金门槛的主线<small>主线解读不依赖三维扫描</small></div>';return;}
 const rulesOpen=!!$('strategyContent').querySelector('.mainline-rules[open]'),openSections=new Set([...$('strategyContent').querySelectorAll('[data-strategy-section][open]')].map(el=>el.dataset.strategySection));$('strategyContent').innerHTML=mainlineStrategyHTML(state.market);if(rulesOpen)$('strategyContent').querySelector('.mainline-rules').open=true;$('strategyContent').querySelectorAll('[data-strategy-section]').forEach(el=>{el.open=openSections.has(el.dataset.strategySection);});
}
function renderWatch(){const items=watchItems();$('watchCount').textContent=items.length;$('watchList').innerHTML=items.length?items.map(s=>`<div class="watch-entry"><div class="list-item ${state.selected.watch===s.code?'selected':''}"><button class="pick" data-action="watch-detail" data-code="${esc(s.code)}"><strong>${esc(s.name||s.code)}</strong><small>${esc(s.code)} · ${esc(s.board||'点击获取日线')}</small></button><button class="icon-button" data-action="remove" data-code="${esc(s.code)}" aria-label="移除 ${esc(s.name||s.code)}">×</button></div>${watchAlertControls(s)}</div>`).join(''):'<div class="empty compact">观察池还是空的<small>添加股票代码，开始你的研究</small></div>';}
const stockSearchState={token:0,items:[],active:-1,pending:null,timer:null,composing:false};
function clearStockSearch(){clearTimeout(stockSearchState.timer);stockSearchState.token++;stockSearchState.items=[];stockSearchState.active=-1;stockSearchState.pending=null;$('stockSearch').hidden=true;$('stockSearchList').innerHTML='';$('stockSearchStatus').textContent='';$('stockCode').setAttribute('aria-expanded','false');$('stockCode').removeAttribute('aria-activedescendant');}
function renderStockSearch(){const items=stockSearchState.items,watch=new Set(watchItems().map(s=>s.code));$('stockSearchList').innerHTML=items.map((s,i)=>`<button type="button" role="option" id="stock-option-${i}" aria-selected="${i===stockSearchState.active}" data-search-index="${i}" class="search-option ${i===stockSearchState.active?'active':''}"><span><strong>${esc(s.name)}</strong><small>${esc(s.code)} · ${s.exchange==='sh'?'沪市':s.exchange==='sz'?'深市':s.exchange==='bj'?'北交所':'演示样本'}</small></span><span class="search-add">${watch.has(s.code)?'已添加':'＋ 自选'}</span></button>`).join('');if(stockSearchState.active>=0)$('stockCode').setAttribute('aria-activedescendant',`stock-option-${stockSearchState.active}`);else $('stockCode').removeAttribute('aria-activedescendant');}
async function searchStocks(){const query=$('stockCode').value.trim();if(!query){clearStockSearch();return [];}const mode=state.mode,gen=state.generation,key=`${mode}:${query}`;if(stockSearchState.pending?.key===key)return stockSearchState.pending.promise;const token=++stockSearchState.token;stockSearchState.items=[];stockSearchState.active=-1;$('stockSearch').hidden=false;$('stockCode').setAttribute('aria-expanded','true');$('stockCode').removeAttribute('aria-activedescendant');$('stockSearchStatus').textContent='正在查找股票…';$('stockSearchList').innerHTML='';
 const pending={key};stockSearchState.pending=pending;pending.promise=(async()=>{try{const result=await api(`/api/terminal/search?${new URLSearchParams({q:query,mode})}`);if(token!==stockSearchState.token||mode!==state.mode||gen!==state.generation||query!==$('stockCode').value.trim())return [];stockSearchState.items=result.items;renderStockSearch();$('stockSearchStatus').textContent=result.items.length?`找到 ${result.items.length} 个匹配 · 点击加入自选`:'未找到匹配的 A 股，试试完整简称或六位代码';return result.items;}catch(error){if(token===stockSearchState.token&&mode===state.mode)$('stockSearchStatus').textContent=error.message;return [];}finally{if(stockSearchState.pending===pending)stockSearchState.pending=null;}})();return pending.promise;}
function scheduleStockSearch(){clearStockSearch();if(stockSearchState.composing||!$('stockCode').value.trim())return;stockSearchState.timer=setTimeout(searchStocks,280);}
async function submitStockSearch(){if(stockSearchState.composing)return;clearTimeout(stockSearchState.timer);const query=$('stockCode').value.trim();if(!query){toast('请输入股票名称或代码');$('stockCode').focus();return;}if(/^[034689]\d{5}$/.test(query)){addWatch(query,stockSearchState.items.find(s=>s.code===query)||null);return;}const mode=state.mode,gen=state.generation,items=await searchStocks();if(mode!==state.mode||gen!==state.generation||query!==$('stockCode').value.trim())return;const exact=items.filter(s=>s.name.toLowerCase()===query.toLowerCase());if(exact.length===1)addWatch(exact[0].code,exact[0]);else if(items.length===1)addWatch(items[0].code,items[0]);else if(items.length>1)toast('找到多只股票，请从匹配列表中选择');}
$('stockCode').addEventListener('input',scheduleStockSearch);
$('stockCode').addEventListener('compositionstart',()=>{stockSearchState.composing=true;clearStockSearch();});
$('stockCode').addEventListener('compositionend',()=>{stockSearchState.composing=false;scheduleStockSearch();});
$('stockCode').addEventListener('keydown',event=>{if(event.isComposing||stockSearchState.composing)return;if(event.key==='Escape'){clearStockSearch();return;}const items=stockSearchState.items;if(!items.length)return;if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();stockSearchState.active=(stockSearchState.active+(event.key==='ArrowDown'?1:stockSearchState.active<0?0:-1)+items.length)%items.length;renderStockSearch();}else if(event.key==='Enter'&&stockSearchState.active>=0){event.preventDefault();const item=items[stockSearchState.active];addWatch(item.code,item);}});
$('stockSearchList').addEventListener('click',event=>{const button=event.target.closest('[data-search-index]');if(!button)return;const item=stockSearchState.items[Number(button.dataset.searchIndex)];if(item)addWatch(item.code,item);});
async function addWatch(code,chosen=null){if(!/^[034689]\d{5}$/.test(code)){toast('请输入沪深京 A 股六位数字代码');return;}let items=watchItems();if(items.some(s=>s.code===code)){toast('该股票已在观察池');return;}if(items.length>=50){toast('观察池最多保存 50 只股票');return;}const meta=chosen||stockMeta(code);items.push({code,name:meta.name,board:meta.board});saveWatch(items);$('stockCode').value='';clearStockSearch();toast('已加入观察池');if(state.page==='watch')showDetail(code,'watch');realtimeLoop?.run('alerts',{force:true});}
function renderScan(){ $('scanCount').textContent=`${state.scan.length} 只已评分`;$('scanList').innerHTML=state.scan.length?state.scan.map((s,i)=>`<div class="list-item ${state.selected.scan===s.code?'selected':''}"><span class="rank">${String(i+1).padStart(2,'0')}</span><button class="pick" data-action="scan-detail" data-code="${esc(s.code)}"><strong>${esc(s.name)}</strong><small>${esc(s.board)} · ${pct(s.pct)}</small></button><span class="list-score">${fmt(s.score,1)}</span></div>`).join(''):'<div class="empty compact">暂无有效评分<small>数据不足的候选不会被赋予零分</small></div>';}
async function startScan({background=false}={}){
 if(state.scanRunning)return;state.scanRunning=true;$('startScan').disabled=true;
 const selected=state.selected.scan,hadDetail=!!selected,gen=state.generation,weights=[...state.weights];
 if(!hadDetail)$('scanDetail').innerHTML='<div class="empty loading">正在按当前权重计算…</div>';
 $('scanProgress').textContent=background?'自动重扫中，保留上一轮结果…':'正在建立扫描任务…';
 try{
  const job=await api('/api/terminal/scan',{method:'POST',body:JSON.stringify({mode:state.mode,weights,min_board_pct:Number($('minBoardPct').value),min_companies:Number($('minCompanies').value)})});
  let done=false;
  while(!done){
   const result=await api(`/api/terminal/scan/${job.id}`);if(gen!==state.generation)return;
   state.boardChecks=result.board_checks||[];renderBoardChecks();
   if(result.status!=='failed'&&(result.results.length||result.status==='complete')){state.scan=result.results.filter(s=>Number.isFinite(s.score)&&s.score>=85);renderScan();}
   $('scanProgress').textContent=`${result.phase||'扫描中'} · ${result.completed} / ${result.total||'…'} · 权重 ${result.weights.map(w=>fmt(w*100,2)+'%').join(' / ')}${result.finished_at?' · 完成 '+result.finished_at.slice(11):''}${result.errors.length?' · '+result.errors.join('；'):''}`;
   done=result.status!=='running';if(result.status==='failed')throw Error(result.errors.join('；'));
   if(!done)await new Promise(resolve=>setTimeout(resolve,900));
  }
  const current=state.selected.scan;
  if(current&&state.scan.some(stock=>stock.code===current)){await showDetail(current,'scan',{background:true,force:true});}
  else if(state.scan.length){await showDetail(state.scan[0].code,'scan');}
  else{state.selected.scan=null;state.detailTokens.scan++;$('scanDetail').innerHTML='<div class="empty">本轮暂无综合分 ≥85 的达标个股<small>不展示低分候选，不自动降低门槛</small></div>';}
 }catch(error){if(gen===state.generation)$('scanProgress').textContent=`更新失败：${error.message}；${state.scan.length?'保留上一轮结果':'稍后自动重试'}`;}
 finally{if(gen===state.generation){state.scanRunning=false;$('startScan').disabled=false;}}
}
async function showDetail(code,target='scan',{background=false,force=false}={}){
 const gen=state.generation;state.detailBusy??={};if(state.detailBusy[target]?.code===code&&state.detailBusy[target]?.gen===gen)return;const pending={code,gen};state.detailBusy[target]=pending;const token=++state.detailTokens[target],scroll=window.scrollY;
 state.selected[target]=code;target==='watch'?renderWatch():renderScan();
 const container=$(target+'Detail');if(!background||!container.querySelector('.detail-header'))container.innerHTML='<div class="empty loading">正在更新个股数据与操作指南…</div>';
 const meta=stockMeta(code),appliedWeights=target==='scan'?(state.scan.find(s=>s.code===code)?.weights||state.weights):[40,30,30];
 const params=new URLSearchParams({mode:state.mode,name:meta.name,board:meta.board,...Object.fromEntries(appliedWeights.map((w,i)=>['w'+(i+1),w]))}),key=`${code}:${params}`;
 try{
  const cached=force?null:state.details.get(key);
  const [dailyResult,minuteResult]=await Promise.allSettled([cached?Promise.resolve(cached):api(`/api/terminal/stock/${code}?${params}`),api(`/api/terminal/intraday/${code}?mode=${state.mode}`)]);
  if(dailyResult.status==='rejected')throw dailyResult.reason;
  let detail={...dailyResult.value,minute:minuteResult.status==='fulfilled'?minuteResult.value:null,minute_error:minuteResult.status==='rejected'?minuteResult.reason.message:null};
  if(detail.minute)detail.quote_date=detail.minute.date;
  state.details.set(key,detail);
  if(gen!==state.generation||token!==state.detailTokens[target])return;
  if(target==='scan'&&(!Number.isFinite(detail.score)||detail.score<85)){state.scan=state.scan.filter(s=>s.code!==code);state.selected.scan=null;renderScan();container.innerHTML='<div class="empty">该股票最新综合分低于85，已移出榜单<small>等待下一轮扫描，或选择其他达标个股</small></div>';return;}
  const openSections=background?[...container.querySelectorAll('details[open]')].map(el=>el.querySelector('summary')?.textContent):[];const projectionOpen=background&&!!container.querySelector('.projection[open]');syncWatchName(detail);container.innerHTML=detailHTML(detail,target);bindChart(container,detail);container.querySelectorAll('details').forEach(el=>{if(openSections.includes(el.querySelector('summary')?.textContent))el.open=true;});if(projectionOpen)container.querySelector('.projection').open=true;if(background)window.scrollTo({top:scroll,behavior:'auto'});
 }catch(error){
  if(gen!==state.generation||token!==state.detailTokens[target])return;
  if(background&&container.querySelector('.detail-header')){container.querySelector('.detail-stale')?.remove();container.insertAdjacentHTML('afterbegin',`<div class="detail-stale">更新失败，保留上次结果：${esc(error.message)}</div>`);}
  else container.innerHTML=`<div class="error-box"><h3>暂时无法读取该股票</h3><p>${esc(error.message)}</p><button class="button" data-action="${target}-detail" data-code="${esc(code)}">重试</button></div>`;
 }finally{if(state.detailBusy[target]===pending)delete state.detailBusy[target];}
}
const detailTabs = {watch:'overview',scan:'overview'};
function detailHTML(s,target='watch'){
 const p=s.plan,last=s.minute?.points?.at(-1),e=evaluatePlan(p,s.minute),active=detailTabs[target]||'overview';
 const tabs=[['overview','决策概览'],['minute','分时信号'],['daily','日 K / 因子'],['funds','主力资金'],['plan','计划与规则']];
 const pane=(key,html)=>`<section class="detail-tab-panel" role="tabpanel" id="${target}-pane-${key}" aria-labelledby="${target}-tab-${key}" data-detail-pane="${key}" ${active===key?'':'hidden'}>${html}</section>`;
 const metrics=[['第一压力',pressureTargetText(p,0)],['止损参考',fmt(p.stop)],['计划盈亏比',p.rr==null?'—':fmt(p.rr)+':1'],['现价剩余盈亏比',e.rr==null?'—':fmt(e.rr)+':1']];
 const overview=`<section class="panel compact-decision"><div class="panel-title"><h2>价格空间</h2><span class="badge">${esc(e.status)}</span></div><div class="decision-metrics">${metrics.map(([label,value])=>`<div><small>${label}</small><strong class="num">${value}</strong></div>`).join('')}</div><div class="compact-note">分钟 ${esc(e.asOf||'待获取')} · 日线计划 ${esc(p.date)} ${esc(p.as_of||'')}<br>已触及目标或止损的旧计划不再给出入场盈亏比；只核对可用分钟收盘。</div></section>${dailyGuideHTML(s)}<details class="panel compact-disclosure"><summary>数据时间与口径 · 日线 / 分钟 / 资金</summary>${dataBasisHTML(s)}</details>`;
 const daily=`<div class="factors">${s.scores.map((v,i)=>`<article class="factor"><div class="factor-head"><span>${factorNames[i]}</span><strong class="num">${fmt(v,0)}</strong></div><div class="factor-bar"><span style="width:${v}%"></span></div><p>${esc(s.reasons[i])}</p></article>`).join('')}</div><section class="panel"><div class="chart-head"><h2>日 K · 量价结构</h2><span class="muted">MA5 / MA10 / MA20</span></div><div class="chart-wrap">${chartSVG(s)}</div><div class="chart-tip">移动到 K 线上查看当日价格</div><p class="chart-caption">日线截至 ${esc(s.date)} · 不复权 · 金线首压力，绿线止损</p></section>`;
 const rules=`${pressureHTML(p,s.name,s.minute)}<details class="panel compact-disclosure"><summary>技术结构与计算规则</summary><div class="analysis"><p>${esc(s.reasons.join('。'))}。</p><p>综合 ${fmt(s.score,1)} 分 · 权重 ${s.weights.map(w=>fmt(w*100,1)+'%').join(' / ')}。</p><p>前20日区间 ${fmt(s.support)}–${fmt(s.resistance)}；VWAP5 ${fmt(s.vwap5)}。结构为区间近似模型。</p></div></details><details class="panel compact-disclosure"><summary>历史信号 · 放量上穿 MA20（${s.signals.length}）</summary><div class="signal-list">${s.signals.map(x=>`<span>${esc(x.date)} <b>${fmt(x.price)}</b></span>`).join('')||'当前范围内未触发'}</div></details>`;
 return `<div class="panel detail-identity"><div class="detail-header"><div><div class="detail-title">${esc(s.name)}<small>${esc(s.code)}</small></div><div class="detail-date">${esc(s.board)} · 日线 ${esc(s.date)} · 来源：${esc(s.source)}${s.source_fallback?' · 已切备用':''}</div></div><div><div class="quote"><strong class="num">${fmt(last?.price)}</strong><span>${last?'分钟最新可用价':'分钟报价待获取'}</span></div><button class="text-button" data-action="add" data-code="${esc(s.code)}">☆ 加入观察池</button></div></div></div><div class="detail-tabs" role="tablist" aria-label="${esc(s.name)}研究分区">${tabs.map(([key,label])=>`<button role="tab" id="${target}-tab-${key}" aria-controls="${target}-pane-${key}" aria-selected="${active===key}" data-detail-tab="${key}" data-detail-target="${target}">${label}</button>`).join('')}</div>${pane('overview',overview)}${pane('minute',s.minute?`<section class="panel detail-minute">${intradayHTML(s.minute)}</section>`:insufficientMinuteHTML(s.minute_error))}${pane('daily',daily)}${pane('funds',fundsPanel(s))}${pane('plan',rules)}<div class="compact-source">${esc(s.source)} · 获取 ${esc(s.retrieved_at)} · ${esc(s.source_note||'')}${s.source_errors?.length?' · 切换原因：'+esc(s.source_errors.join('；')):''}${s.demo?' · 演示样本':''}</div>`;
}
document.addEventListener('click',event=>{
 const button=event.target.closest('[data-detail-tab]');if(!button)return;
 const target=button.dataset.detailTarget,key=button.dataset.detailTab,container=$(target+'Detail');detailTabs[target]=key;
 container.querySelectorAll('[data-detail-tab]').forEach(el=>el.setAttribute('aria-selected',String(el.dataset.detailTab===key)));
 container.querySelectorAll('[data-detail-pane]').forEach(el=>el.hidden=el.dataset.detailPane!==key);
});
function chartSVG(s){const bars=s.bars;const W=980,H=365,left=12,right=81,top=20,bottom=300;const inner=W-left-right;const values=bars.flatMap(b=>[b.high,b.low,b.ma5,b.ma10,b.ma20]).filter(Number.isFinite);values.push(...[s.plan.target1,s.plan.stop,...(s.plan.levels||[]).map(level=>level.high)].filter(Number.isFinite));let low=Math.min(...values),high=Math.max(...values);const pad=(high-low)*.08||1;low-=pad;high+=pad;const y=v=>top+(high-v)/(high-low)*(bottom-top);const step=inner/bars.length;const x=i=>left+(i+.5)*step;const maxVol=Math.max(...bars.map(b=>b.volume),1);let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(s.name)}日线图">`;
for(let i=0;i<=4;i++){const value=low+(high-low)*i/4,yy=y(value);svg+=`<line x1="${left}" x2="${W-right}" y1="${yy}" y2="${yy}" stroke="#2a3442" stroke-width=".7"/><text x="${W-right+10}" y="${yy+4}" fill="#8996a9" font-size="10" font-family="monospace">${fmt(value)}</text>`;}
for(const [i,b] of bars.entries()){const color=b.close>=b.open?'#f16a77':'#4bc4a2';const bw=Math.max(1,step*.58);svg+=`<line x1="${x(i)}" x2="${x(i)}" y1="${y(b.high)}" y2="${y(b.low)}" stroke="${color}"/><rect x="${x(i)-bw/2}" y="${Math.min(y(b.open),y(b.close))}" width="${bw}" height="${Math.max(1,Math.abs(y(b.open)-y(b.close)))}" fill="${color}"/><rect x="${x(i)-bw/2}" y="${340-b.volume/maxVol*29}" width="${bw}" height="${b.volume/maxVol*29}" fill="${color}" opacity=".4"/>`;if(i%25===0)svg+=`<text x="${x(i)}" y="359" fill="#8996a9" font-size="9" font-family="monospace">${b.date.slice(5)}</text>`;}
for(const [key,color] of [['ma5','#e9edf3'],['ma10','#d8b477'],['ma20','#79a9e8']]){const points=bars.map((b,i)=>Number.isFinite(b[key])?`${x(i)},${y(b[key])}`:null).filter(Boolean).join(' ');svg+=`<polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.4"/>`;}
for(const [value,label,color] of [[s.plan.target1,'首压力','#d8b477'],[s.plan.stop,'止损','#4bc4a2']]){if(!Number.isFinite(value))continue;svg+=`<line x1="${left}" x2="${W-right}" y1="${y(value)}" y2="${y(value)}" stroke="${color}" stroke-width=".7" stroke-dasharray="3 4"/><text x="${W-right+7}" y="${y(value)-5}" fill="${color}" font-size="9">${label} ${fmt(value)}</text>`;}
for(const [i,b] of bars.entries())svg+=`<rect class="chart-hit" data-bar="${i}" x="${left+i*step}" y="${top}" width="${step}" height="${bottom-top}" fill="transparent"/>`;return svg+'</svg>';}
function bindChart(container,s){container.querySelectorAll('[data-bar]').forEach(el=>{el.addEventListener('pointerenter',()=>{const b=s.bars[Number(el.dataset.bar)];container.querySelector('.chart-tip').textContent=`${b.date}　开 ${fmt(b.open)}　高 ${fmt(b.high)}　低 ${fmt(b.low)}　收 ${fmt(b.close)}　量 ${fmt(b.volume,0)} 手`;});});}
document.addEventListener('click',async event=>{const nav=event.target.closest('[data-page]');if(nav){navigate(nav.dataset.page);return;}const button=event.target.closest('[data-action]');if(!button)return;const {action,code}=button.dataset;if(action==='demo'){await changeMode('demo');}if(action==='retry')realtimeLoop.run('market',{force:true});if(action==='main-intraday')loadMainIntraday(code,button);if(action==='detail'){navigate('watch');showDetail(code,'watch');}if(action==='alert-edit')openAlertEditor(code);if(action==='watch-detail')showDetail(code,'watch');if(action==='scan-detail')showDetail(code,'scan');if(action==='scan-page')navigate('scan');if(action==='add')addWatch(code);if(action==='remove'){saveWatch(watchItems().filter(s=>s.code!==code));if(state.selected.watch===code){state.selected.watch=null;state.detailTokens.watch++;$('watchDetail').innerHTML='<div class="empty">选择股票，继续研究</div>';}}});
$('refresh').addEventListener('click',()=>{if(state.page==='sim'){loadStrategySim();return;}if(state.page==='paper')loadPaper();state.details.clear();realtimeLoop.run('market',{force:true});if(state.page==='main')realtimeLoop.run('minutes',{force:true});if(state.page==='watch')realtimeLoop.run('detail',{force:true});if(state.page==='scan'){realtimeLoop.run('scan',{force:true});if(state.selected.scan)realtimeLoop.run('minutes',{force:true});}});$('mode').addEventListener('change',e=>changeMode(e.target.value));$('addForm').addEventListener('submit',e=>{e.preventDefault();submitStockSearch();});$('startScan').addEventListener('click',()=>realtimeLoop.run('scan',{force:true}));$('toggleWeights').addEventListener('click',()=>{const hidden=!$('weightsPanel').hidden;$('weightsPanel').hidden=hidden;$('toggleWeights').setAttribute('aria-expanded',String(!hidden));});$('weightInputs').innerHTML=factorNames.map((name,i)=>`<label>${name}<input type="range" min="0" max="100" value="${state.weights[i]}" data-weight="${i}" aria-label="${name}权重"><output>${state.weights[i]}</output></label>`).join('');document.querySelectorAll('[data-weight]').forEach(input=>input.addEventListener('input',()=>{state.weights[Number(input.dataset.weight)]=Number(input.value);input.nextElementSibling.textContent=input.value;}));$('showRules').addEventListener('click',()=>$('rulesDialog').showModal());$('closeRules').addEventListener('click',()=>$('rulesDialog').close());$('clock').textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',weekday:'long'}).format(new Date());updateMode();renderWatch();

function renderBoardChecks(){
 $('boardChecks').innerHTML=state.boardChecks.length?`<div class="gate-summary"><span>板块共振检验</span><span class="muted">最多 8 个达标涨幅行业 × 各前 20 只成分 · 只统计已取得日线的样本</span></div><div class="gate-grid">${state.boardChecks.map(check=>`<article class="gate-card ${check.passed?'passed':''}"><div><strong>${esc(check.name)}</strong><span class="num ${sign(check.pct)}">${pct(check.pct)}</span></div><p>动量达标 <b class="num">${check.qualified}</b> / 已分析 ${check.analyzed} / 采样 ${check.sampled}</p><span class="badge">${esc(check.status||(check.passed?'已过门槛':'检验中'))}</span>${check.errors?`<small class="muted"> ${check.errors} 只数据不可用</small>`:''}</article>`).join('')}</div>`:'';
}
async function loadMainIntraday(code,button){
 const body=button.closest('[data-main-code]').querySelector('.intraday-body');
 body.hidden=!body.hidden;button.setAttribute('aria-expanded',String(!body.hidden));button.textContent=body.hidden?'展开分时与信号 ↓':'收起分时与信号 ↑';
 if(!body.hidden)await fetchMainIntraday(code,body);
}
async function fetchMainIntraday(code,body,{force=false}={}){
 if(body._pending)return body._pending;if(!force&&body._snapshot&&Date.now()-(body._fetchedAt||0)<25000)return;
 const gen=state.generation;if(!body._snapshot)body.innerHTML='<div class="empty compact loading">正在读取分钟行情…</div>';
 body._pending=(async()=>{
  try{
   const data=await api(`/api/terminal/intraday/${code}?mode=${state.mode}`);if(gen!==state.generation||!body.isConnected)return;
   const scroll=window.scrollY,projectionOpen=!!body.querySelector('.projection[open]');body._snapshot=data;body._fetchedAt=Date.now();body.innerHTML=intradayHTML(data);if(projectionOpen)body.querySelector('.projection').open=true;
   body.closest('[data-main-code]').querySelector('.main-operation').innerHTML=mainGuideHTML(stockMeta(code),data);window.scrollTo({top:scroll,behavior:'auto'});
  }catch(error){
   if(gen!==state.generation||!body.isConnected)return;
   body.querySelector('.intraday-error')?.remove();
   if(body._snapshot)body.insertAdjacentHTML('afterbegin',`<div class="intraday-error">分时更新失败，保留上次数据：${esc(error.message)}</div>`);
   else body.innerHTML=`<div class="empty compact">${esc(error.message)}<small>稍后自动重试，也可收起后重新展开</small></div>`;
  }finally{body._pending=null;}
 })();return body._pending;
}
function insufficientMinuteHTML(reason){return `<section class="panel"><p class="chart-caption">分钟更新失败：${esc(reason||'暂不可用')}。日线分析保留，最新报价及现价盈亏比留空，稍后自动重试。</p></section>`;}
function intradayHTML(data){
 const p=data.plan;
 return `<div class="chart-head"><h2>分时 · 量价信号</h2><span class="muted">${esc(data.source)}${data.source_fallback?' · 已切备用':''} · ${esc(data.date)} · 最新可用分钟 ${esc(data.as_of||data.date)}</span></div><div class="chart-wrap">${intradaySVG(data)}</div><div class="chart-caption">白线：价格 · 金线：累计成交均价 · 底部：分钟成交量。信号按该分钟收盘确认；均量只取此前5根，不以后续数据回标。未经收益验证。</div><div class="plan-grid"><div class="plan-item"><small>首个分时信号 / 参考价</small><strong>${fmt(p.reference)}</strong></div><div class="plan-item"><small>第一压力位</small><strong class="up">${fmt(p.target1)}</strong></div><div class="plan-item"><small>参考止损 -3%</small><strong class="down">${fmt(p.stop)}</strong></div><div class="plan-item"><small>首压力盈亏比</small><strong>${p.rr===null?'—':fmt(p.rr)+':1'}</strong></div></div><details class="panel compact-disclosure"><summary>分钟压力位与现价盈亏比详情</summary>${pressureHTML(p,stockMeta(data.code).name,data)}</details><details class="panel compact-disclosure"><summary>开盘量能 · 非集合竞价</summary>${openingHTML(data)}</details><details class="panel compact-disclosure"><summary>已确认分时信号（${data.signals.length}）</summary><div class="intraday-signals">${data.signals.map(signal=>`<div><span class="role">${esc(signal.kind)}</span><span class="num">${esc(signal.time)} 确认 · ${fmt(signal.price)}</span><span class="muted">${esc(signal.reason)}</span></div>`).join('')||'<p>当前分时数据未触发规则信号，不生成参考价格。</p>'}</div></details><div class="chart-caption">${esc(data.source)} · 获取 ${esc(data.retrieved_at)} · ${esc(data.source_note||'')}${data.source_errors?.length?' · 切换原因：'+esc(data.source_errors.join('；')):''} · 分时参考与日线研究计划分别计算</div>`;
}
function intradaySVG(data){
 const points=data.points,W=980,H=285,left=15,right=72,top=20,bottom=225;
 const values=points.flatMap(p=>[p.price,p.average]).filter(Number.isFinite);values.push(...[data.plan.target1,data.plan.stop].filter(Number.isFinite));
 let lo=Math.min(...values),hi=Math.max(...values),pad=(hi-lo)*.15||1;lo-=pad;hi+=pad;
 const x=i=>left+i/Math.max(1,points.length-1)*(W-left-right),y=v=>top+(hi-v)/(hi-lo)*(bottom-top);
 let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="当日价格与均价分时图">`;
 for(let i=0;i<=4;i++){const value=lo+(hi-lo)*i/4;svg+=`<line x1="${left}" x2="${W-right}" y1="${y(value)}" y2="${y(value)}" stroke="#2a3442"/><text x="${W-right+10}" y="${y(value)+4}" fill="#8996a9" font-size="10">${fmt(value)}</text>`;}
 for(const [key,color] of [['price','#e8edf4'],['average','#d8b477']])svg+=`<polyline points="${points.map((p,i)=>Number.isFinite(p[key])?`${x(i)},${y(p[key])}`:null).filter(Boolean).join(' ')}" fill="none" stroke="${color}" stroke-width="1.6"/>`;
 for(const [value,label,color] of [[data.plan.target1,'首压力','#d8b477'],[data.plan.stop,'止损','#4bc4a2']]){if(!Number.isFinite(value))continue;svg+=`<line x1="${left}" x2="${W-right}" y1="${y(value)}" y2="${y(value)}" stroke="${color}" stroke-dasharray="4 4"/><text x="${W-right+5}" y="${y(value)-4}" fill="${color}" font-size="9">${label} ${fmt(value)}</text>`;}
 const maxVolume=Math.max(...points.map(p=>p.volume),1);
 points.forEach((p,i)=>{svg+=`<rect x="${x(i)}" y="${260-p.volume/maxVolume*25}" width="2" height="${p.volume/maxVolume*25}" fill="#d8b477" opacity=".4"/>`;if(i%60===0)svg+=`<text x="${x(i)}" y="280" font-size="9" fill="#8996a9">${esc(p.time)}</text>`;});
 data.signals.forEach(signal=>{const i=points.findIndex(p=>p.time===signal.time);if(i>=0)svg+=`<circle cx="${x(i)}" cy="${y(signal.price)}" r="4" fill="#4bc4a2"><title>${esc(signal.kind)} ${esc(signal.time)}</title></circle>`;});
 return svg+'</svg>';
}

function mainGuideHTML(stock,minute=null){
 const role=stock.role||'个股观察',last=minute?.points.at(-1),signals=minute?.signals||[];
 const above=last&&last.average!==null?last.price>=last.average:null;
 let condition=role.includes('龙头')?'观察涨停封板、连板梯队、主力净流入及分时量价信号':role.includes('趋势')?'观察连续净流入、成交活跃度及价格能否保持在分时均价上方':'观察相对涨幅较低的个股是否出现上穿均价或放量信号';
 const status=minute?evaluatePlan(minute.plan,minute).status:'等待分时确认';
 const guideOpen=!!document.querySelector(`[data-main-code="${stock.code}"] .main-guide-fold[open]`);
 return `<details class="main-guide-fold" ${guideOpen?'open':''}><summary>操作指南 <span>${esc(status)}</span></summary><section class="operation-guide"><div class="guide-head"><h3>操作指南 <span class="muted">${esc(stock.name||stock.code)}</span></h3><span class="badge">${status}</span></div><p><b>观察条件</b>${condition}。${esc(stock.role_reason||'')}</p><p><b>资金核对</b>${esc(fundsEvidence(stock))}</p><p><b>当前依据</b>涨幅 ${pct(stock.pct)}，量比 ${fmt(stock.volume_ratio)}，振幅 ${fmt(stock.amplitude)}%${last?`；分时价 ${fmt(last.price)} / 均价 ${fmt(last.average)}`:''}。</p>${minute&&minute.plan.reference!==null?`<p><b>信号参考</b>${esc(signals[0].time)} ${esc(signals[0].kind)}，参考价 ${fmt(minute.plan.reference)}；首压力 ${pressureTargetText(minute.plan,0)}，失效参考 ${fmt(minute.plan.stop)}；首压力盈亏比 ${minute.plan.rr===null?'暂不可计算':fmt(minute.plan.rr)+':1'}。</p>`:'<p><b>下一步</b>展开分时查看触发条件；没有已触发信号时不生成参考价格。</p>'}<small>基于当前数据的规则说明；信号不代表可按该价成交。</small></section></details>`;
}
function dailyGuideHTML(stock){
 const plan=stock.plan,status=evaluatePlan(plan,stock.minute).status;
 return `<section class="panel operation-guide daily-operation"><div class="guide-head"><h3>操作指南 <span class="muted">${esc(stock.name)} · 日线结构依据</span></h3><span class="badge">${esc(status)}</span></div><p><b>观察条件</b>${esc(stock.reasons.join('。'))}。</p><p><b>资金核对</b>${esc(fundsEvidence(stock))}</p><p><b>参考区间</b>${fmt(plan.entry_low)}–${fmt(plan.entry_high)}；参考价 ${fmt(plan.reference)}，以 ${esc(plan.date)} 末根日线为基准。</p><p><b>压力与风险</b>第一压力 ${pressureTargetText(plan,0)}，第二压力 ${pressureTargetText(plan,1)}；失效参考 ${fmt(plan.stop)}。${plan.rr===null?'无明确上方压力位，暂不计算压力盈亏比。':`首压力盈亏比 ${fmt(plan.rr)}:1，${esc(plan.assessment)}。`}</p><p><b>下一步</b>结合分钟行情确认信号；日线压力与分钟线压力分别计算；角色不等于入场结论，按现价空间与计划状态复核。</p><small>随个股数据更新；价格结构为规则参考，未验证交易收益。</small></section>`;
}
const savedInterval=Number(stored('sy-auto-seconds',30));
$('refreshInterval').value=String([0,30,60].includes(savedInterval)?savedInterval:30);
function intervalMs(){return Number($('refreshInterval').value)*1000;}
function renderAutoStatus(){
 const interval=intervalMs();
 if(state.page==='sim'){$('autoStatus').textContent='策略引擎在服务后台运行 · 约30秒检查 · 页面约10秒更新';return;}
 if(state.page==='paper'){$('autoStatus').textContent='模拟引擎在本机服务后台运行 · 约30秒核对 · '+(interval?'账本页面约10秒刷新':'页面刷新暂停，模拟引擎继续运行');return;}
 if(!interval){$('autoStatus').textContent='自动刷新已暂停 · 可手动刷新';return;}
 if(document.hidden){$('autoStatus').textContent='页面在后台 · 自动刷新暂停';return;}
 if(!realtimeLoop)return;
 const task=state.page==='scan'?'scan':state.page==='watch'?'detail':'market';
 if(task==='detail'&&!state.selected.watch){$('autoStatus').textContent=`自动刷新 ${interval/1000} 秒 · 全部自选自动监测，选择个股可看详情`;return;}
 const seconds=realtimeLoop.secondsUntil(task);
 $('autoStatus').textContent=realtimeLoop.running.has(task)?`正在自动更新${task==='scan'?'三维扫描':task==='detail'?'个股与操作指南':'主线行情'}…`:`自动刷新已开启 · ${task==='scan'?'重扫':'行情'} ${seconds} 秒后 · 分时 ${interval/1000}秒 / 重扫 ${Math.max(60,interval/500)}秒`;
}
realtimeLoop=new RefreshLoop({interval:intervalMs,visible:()=>!document.hidden,onStatus:renderAutoStatus,tasks:{
 market:{delay:n=>n,eligible:()=>state.page==='main'||state.page==='strategy',run:()=>refresh({background:true})},
 minutes:{delay:n=>n,eligible:()=>state.page==='scan'?!!state.selected.scan:state.page==='main'&&!!document.querySelector('[data-main-code] .intraday-body:not([hidden])'),run:()=>state.page==='scan'?showDetail(state.selected.scan,'scan',{background:true,force:true}):Promise.allSettled([...document.querySelectorAll('[data-main-code]')].filter(article=>!article.querySelector('.intraday-body').hidden).map(article=>fetchMainIntraday(article.dataset.mainCode,article.querySelector('.intraday-body'),{force:true})))},
 paper:{delay:()=>10000,eligible:()=>state.page==='paper',run:loadPaper},
 sim:{delay:()=>10000,eligible:()=>true,run:loadStrategySim},
 alerts:{delay:n=>Math.max(30000,n),eligible:()=>watchItems().length>0,run:pollWatchAlerts},
 detail:{delay:n=>n,eligible:()=>state.page==='watch'&&!!state.selected.watch,run:()=>showDetail(state.selected.watch,'watch',{background:true,force:true})},
 scan:{delay:n=>Math.max(60000,n*2),eligible:()=>state.page==='scan'&&!document.activeElement?.matches('#weightsPanel input,#minBoardPct,#minCompanies'),run:()=>startScan({background:true})}
}});
$('refreshInterval').addEventListener('change',()=>{persist('sy-auto-seconds',Number($('refreshInterval').value));realtimeLoop.tick();renderAutoStatus();});
document.addEventListener('visibilitychange',()=>realtimeLoop.tick());
window.addEventListener('pagehide',()=>realtimeLoop.stop());
window.addEventListener('pageshow',()=>realtimeLoop.start());
initNotes();initPaper();initWatchAlerts();initStrategySim();realtimeLoop.start();if(!intervalMs())realtimeLoop.run('market',{force:true});

if(new URLSearchParams(location.search).get("page")==="notes")navigate("notes");

if(new URLSearchParams(location.search).get("page")==="sim")navigate("sim");
