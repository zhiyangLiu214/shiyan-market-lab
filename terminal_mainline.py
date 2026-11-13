"""Fund-led mainlines using one sector taxonomy; no technical scan dependency."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import math

from terminal_engine import DEMO_GROUPS, TZ, demo_history, now_text
from terminal_funds import demo_funds, missing, number

WEIGHTS = {'净流入占比': 40, '净流入规模': 10, '资金持续性': 10,
           '上涨宽度': 15, '板块涨幅': 10, '涨停扩散': 10, '连板高度': 5}
VERSION = 'mainline-funds-v2.1'
MIN_MEMBERS = 10
ROLE_LIMIT = 3
TREND_CHECK_LIMIT = 6


def board_hierarchy(groups):
    """Qualification precedes ranking; main/secondary/trend may all be absent."""
    confirmed = []
    for g in groups:
        f = g['funds']
        aligned = f.get('status') == 'ok' and f.get('date') == g.get('date')
        sustained = (f.get('positive_days') or 0) >= 2 or (f.get('three_day_net') or 0) > 0 and (f.get('five_day_net') or 0) > 0
        current = aligned and (f.get('net') or 0) > 0 and (f.get('ratio') or 0) >= 1 and g.get('pct', 0) > 0
        resonance = current and sustained and g.get('strength', 0) >= 70 and (g.get('breadth') or 0) >= 65 and (g.get('limit_count') or 0) >= 2
        if resonance:
            confirmed.append(g)
        g['_persistent'] = aligned and sustained
    main = confirmed[:2]
    remaining = [g for g in groups if g not in main]
    persistent = [g for g in remaining if g['_persistent'] and (g['funds'].get('net') or 0) > 0 and g.get('pct', 0) > 0]
    trend = max(persistent, key=lambda g: (g['funds'].get('five_day_net') or 0, g['strength']), default=None)
    chosen = main + ([trend] if trend else [])
    chosen += [g for g in remaining if g not in chosen][:3-len(chosen)]
    for i, g in enumerate(chosen):
        kind = ('main' if main.index(g) == 0 else 'secondary') if g in main else 'trend' if g is trend else 'potential' if (g['funds'].get('three_day_net') or 0)>0 or (g['funds'].get('five_day_net') or 0)>0 else 'watch'
        labels = {'main': '已确认主线', 'secondary': '已确认次主线', 'trend': '资金趋势板块', 'potential': '潜在待涨观察', 'watch': '当日资金观察 · 多日待核验'}
        f = g['funds']
        reason = ('当前正向资金、多日累积、价格与涨停扩散通过确认条件；合格板块内再排序' if g in main else
                  '多日资金累积且当日资金与价格为正；涨停共振尚未通过，不等同已确认主线' if g is trend else
                  '近3/5日累计资金为正，尚待价格、当日资金与板块扩散确认' if (f.get('three_day_net') or 0) > 0 or (f.get('five_day_net') or 0) > 0 else
                  '仅有当日资金线索，多日累积尚未确认；不能据此认定吸筹或即将上涨')
        g.update(rank=i+1, board_type=kind, board_label=labels[kind], board_reason=reason)
        g.pop('_persistent', None)
    for g in groups:
        g.pop('_persistent', None)
    return chosen


def percentile(value, universe):
    if len(universe) < 2:
        return 50
    return 100 * (sum(v < value for v in universe) + (sum(v == value for v in universe)-1)/2) / (len(universe)-1)


def merge_history(snapshot, history):
    result = dict(snapshot, positive_days=None, three_day_net=None, five_day_net=None)
    if history.get('status') != 'ok' or not snapshot.get('date'):
        return result
    if history.get('history_live_date') and history['history_live_date']!=snapshot['date']:
        # A prior intraday merge is not a completed daily-history observation.
        return result
    rows = [dict(r) for r in history.get('history', []) if r['date'] <= snapshot['date']]
    if not rows or rows[-1]['date'] != history.get('date'):
        return result
    if rows[-1]['date']==snapshot['date']:
        rows[-1].update(net=snapshot['net'], ratio=snapshot.get('ratio'))
    else:
        # The daily-history service can end at the last completed session.
        # Require the immediately preceding weekday; longer holiday/data gaps
        # remain unconfirmed until an exchange calendar is available.
        previous=datetime.strptime(snapshot['date'],'%Y-%m-%d')-timedelta(days=1)
        while previous.weekday()>=5: previous-=timedelta(days=1)
        if rows[-1]['date']!=previous.strftime('%Y-%m-%d'):return result
        rows.append(dict(date=snapshot['date'],net=snapshot['net'],ratio=snapshot.get('ratio')))
    streak = 0
    for row in reversed(rows):
        if row['net'] <= 0:
            break
        streak += 1
    result.update(history=rows, history_live_date=snapshot['date'], positive_days=streak,
                  history_source=history.get('history_source') or history.get('source'),history_period=history.get('period'),
                  three_day_net=sum(r['net'] for r in rows[-3:]) if len(rows) >= 3 else None,
                  five_day_net=sum(r['net'] for r in rows[-5:]) if len(rows) >= 5 else None)
    return result


def score_sector(group, scales):
    f = group['funds']
    if (f.get('status') != 'ok' or f.get('date') != group.get('date')
            or f.get('net') is None or f.get('ratio') is None or group.get('pct') is None
            or not ((f['net'] > 0 and f['ratio'] > 0) or (f.get('three_day_net') or 0) > 0 or (f.get('five_day_net') or 0) > 0)):
        return None
    parts = {'净流入占比': min(100, max(0, f['ratio'] / 10 * 100)),
             '净流入规模': percentile(f['net'], scales),
             '资金持续性': min(100, f['positive_days']/3*100) if f.get('positive_days') is not None else None,
             '上涨宽度': group.get('breadth'), '板块涨幅': min(100, max(0, group['pct']/5*100)),
             '涨停扩散': min(100, group['limit_count']/group['member_count']*1000) if group.get('limit_count') is not None else None,
             '连板高度': min(100, group['max_streak']/5*100) if group.get('max_streak') is not None else None}
    coverage = sum(WEIGHTS[k] for k, v in parts.items() if v is not None)
    group.update(strength=round(sum(WEIGHTS[k]*v for k, v in parts.items() if v is not None)/coverage, 1),
                 score_parts={k: {'score': round(v, 1) if v is not None else None, 'weight': WEIGHTS[k]} for k, v in parts.items()},
                 score_coverage=coverage)
    if f['net'] <= 0:
        group['stage'] = '多日累积 · 当日流出分歧'
    elif (f.get('positive_days') or 0) < 2:
        group['stage'] = '资金领先 · 持续性待确认'
    elif group.get('limit_count') is None:
        group['stage'] = '资金持续 · 涨停数据待核验'
    elif group['limit_count'] >= 2 and (group.get('breadth') or 0) >= 65:
        group['stage'] = '资金与涨停共振'
    else:
        group['stage'] = '资金持续 · 涨停扩散待确认'
    return group


def choose_roles(stocks, pool, date):
    """Strict role gates. Missing money data must never become an eligible zero."""
    positive = [dict(s) for s in stocks if s.get('price', 0) > 0 and s.get('pct', 0) > 0
                and s.get('funds', {}).get('status') == 'ok' and s['funds'].get('date') == date
                and (s['funds'].get('net') or 0) > 0 and (s['funds'].get('ratio') or 0) > 0]
    selected, gaps = [], []
    leaders = [s for s in positive if s['code'] in pool]
    leader = max(leaders, key=lambda s: (pool[s['code']]['streak'], -pool[s['code']]['breaks'],
                                         -pool[s['code']]['first_seal'], s['funds']['ratio'], s['funds']['net']), default=None)
    if leader:
        leader.update(role='龙头候选', role_reason=f"涨停池确认，{pool[leader['code']]['streak']} 连板；优先连板高度、少炸板与早封板，主力净流入为正。")
        selected.append(leader)
    else:
        gaps.append('龙头：未找到涨停池与正向资金同时确认的个股')
    used = {s['code'] for s in selected}
    trends = [s for s in positive if s['code'] not in used and (s['funds'].get('positive_days') or 0) >= 2
              and (s.get('volume_ratio') or 0) >= 1]
    trends.sort(key=lambda s: (-(s.get('amount') or 0), -s['funds']['ratio'], s['code']))
    for trend in trends[:ROLE_LIMIT]:
        trend.update(role='趋势龙候选', role_reason=f"主力连续 {trend['funds']['positive_days']} 个交易日净流入、量比 ≥1；符合条件的候选中成交额领先，均线趋势仍需日线确认。")
        selected.append(trend)
    if not trends:
        gaps.append(f'趋势龙：未满足连续两日净流入与量比条件（持续性仅核验成交额前 {TREND_CHECK_LIMIT} 名资金候选）')
    used = {s['code'] for s in selected}
    amounts = [s['amount'] for s in stocks if s.get('amount') is not None]
    average = sum(amounts)/len(amounts) if len(amounts) == len(stocks) and amounts else None
    follows = [s for s in positive if s['code'] not in used and s['code'] not in pool and leader
               and s['pct'] < leader['pct'] and average is not None and (s.get('amount') or 0) > average
               and (s.get('volume_ratio') or 0) >= 1.2]
    follows.sort(key=lambda s: (-s['funds']['ratio'], -s['funds']['net'], s['code']))
    for follow in follows[:ROLE_LIMIT]:
        follow.update(role='补涨观察', role_reason='正向资金确认、量比 ≥1.2、成交额超过完整成分样本均值，涨幅低于龙头；符合条件者优先净流入占比。')
        selected.append(follow)
    if not follows:
        gaps.append('补涨：未同时满足龙头参照、放量、成交额高于板块均值及正向资金条件')
    return selected, gaps


class MainlineService:
    def __init__(self, funds):
        self.funds = funds

    def members(self, code):
        def load():
            def page(pn):
                d = self.funds._json('https://push2.eastmoney.com/api/qt/clist/get',
                    dict(pn=pn, pz=20, np=1, fltt=2, invt=2, fid='f6', po=1, fs='b:'+code,
                         fields='f12,f14,f2,f3,f6,f8,f10,f15,f16,f18,f62,f184,f124')).get('data')
                if not d or not isinstance(d.get('diff'), list):
                    raise ValueError('行业成分为空')
                return d
            first = page(1)
            pages = math.ceil(int(first['total'])/20)
            if not 1 <= pages <= 100:
                raise ValueError('成分股数量超出本轮支持范围')
            rows = first['diff'][:]
            if pages > 1:
                with ThreadPoolExecutor(max_workers=3) as executor:
                    for data in executor.map(page, range(2, pages+1)):
                        rows.extend(data['diff'])
            unique = {r['f12']: r for r in rows}
            if len(unique) != first['total']:
                raise ValueError('成分分页不完整，本板块不参加排名')
            stocks = []
            for r in unique.values():
                stamp = number(r.get('f124'))
                date = datetime.fromtimestamp(stamp, TZ).strftime('%Y-%m-%d') if stamp else None
                price, pct, amount = number(r.get('f2')), number(r.get('f3')), number(r.get('f6'))
                net, ratio = number(r.get('f62')), number(r.get('f184'))
                f = dict(status='ok' if net is not None and ratio is not None and date else 'unavailable',
                         net=net, ratio=ratio, date=date, source='东方财富成分股资金',
                         retrieved_at=now_text(), period='所示交易日累计')
                previous, high, low = number(r.get('f18')), number(r.get('f15')), number(r.get('f16'))
                stocks.append(dict(code=r['f12'], name=r['f14'], price=price or 0, pct=pct or 0,
                                   amount=amount/1e8 if amount is not None else None,
                                   volume_ratio=number(r.get('f10')), quote_date=date, funds=f,
                                   amplitude=round((high-low)/previous*100, 2) if previous and high and low else None))
            return dict(status='ok', items=stocks)
        result = self.funds._cached('members:'+code, load, ttl=60)
        if result['status'] != 'ok':
            raise ValueError('成分资金快照不可用或已陈旧')
        return [dict(s, funds=dict(s['funds'])) for s in result['items']]

    def limit_pool(self, date):
        def load():
            d = self.funds._json('https://push2ex.eastmoney.com/getTopicZTPool',
                                dict(ut='7eea3edcaed734bea9cbfc24409ed989', dpt='wz.ztzt',
                                     Pageindex=0, pagesize=10000, sort='fbt:asc', date=date.replace('-', ''))).get('data')
            if not d or str(d.get('qdate')) != date.replace('-', '') or not isinstance(d.get('pool'), list):
                raise ValueError('涨停池日期不匹配')
            if d.get('tc') is not None and int(d['tc']) != len(d['pool']):
                raise ValueError('涨停池不完整')
            return dict(status='ok', date=date, items={r['c']: dict(streak=int(r['lbc']),
                        breaks=int(r['zbc']), first_seal=int(r['fbt'])) for r in d['pool']})
        return self.funds._cached('limit-pool:'+date, load)

    def build(self):
        source = self.funds.sectors()
        if source['status'] != 'ok':
            raise ValueError('行业资金不可用：'+source.get('reason','等待重试'))
        rows = list(source['items'].values())
        dates = [r['date'] for r in rows if r.get('date')]
        date = max(set(dates), key=dates.count) if dates else None
        universe = []
        # Rotate through all industry histories, retaining recent sectors between refreshes.
        # Fixed daily leaders alone would miss slow accumulation in flat/declining sectors.
        eligible = [r for r in rows if r.get('date') == date]
        history_rows = sorted(eligible, key=lambda r: (-(r.get('net') or 0), r['sector_code']))
        cursor = getattr(self, '_history_cursor', 0)
        batch = history_rows[cursor:cursor+16]
        self._history_cursor = (cursor+16) % max(1,len(history_rows))
        candidates = {r['sector_code']:r for r in history_rows[:8]+batch}
        def enrich_history(r):
            return r['sector_code'], merge_history(r, self.funds.sector_history(r['sector_code']))
        with ThreadPoolExecutor(max_workers=4) as executor:
            updates = dict(executor.map(enrich_history, candidates.values()))
        if not hasattr(self, '_histories'): self._histories = {}
        self._histories.update({code:value for code,value in updates.items() if value.get('history')})
        rows = [merge_history(r, self._histories.get(r['sector_code'], {})) for r in rows]
        scales = [r['net'] for r in rows if r.get('net') is not None and r['net'] > 0 and r['date'] == date]
        for r in rows:
            group = dict(r, funds=dict(r, source='东方财富行业资金'), member_count=1,
                         limit_count=None, max_streak=None)
            widths = [r.get(k) for k in ('rise_count', 'fall_count', 'flat_count')]
            total = sum(widths) if all(v is not None for v in widths) else 0
            group['breadth'] = r['rise_count']/total*100 if total and r.get('rise_count') is not None else None
            if total >= MIN_MEMBERS and r['date'] == date and score_sector(group, scales):
                universe.append(group)
        universe.sort(key=lambda g: (-g['strength'], g['sector_code']))
        # Reserve slots for multi-day accumulation, including a negative current day.
        accumulation = [g for g in universe if (g['funds'].get('three_day_net') or 0)>0 or (g['funds'].get('five_day_net') or 0)>0]
        shortlist = universe[:5] + [g for g in accumulation if g not in universe[:5]][:3]
        if len(shortlist)<8: shortlist += [g for g in universe if g not in shortlist][:8-len(shortlist)]
        errors = []
        if not shortlist:
            return dict(groups=[], errors=['当前无正涨幅且主力净流入及净占比均为正的行业'], date=date,
                        updated_at=now_text(), demo=False, source='东方财富行业、成分股与资金流',
                        scope='资金门槛未通过，不自动退回纯涨幅排名', version=VERSION, weights=WEIGHTS)
        pool = self.limit_pool(date)
        def enrich(group):
            stocks = self.members(group['sector_code'])
            if not any(s['quote_date'] == date for s in stocks):
                raise ValueError('成分股与板块日期不一致')
            codes = {s['code'] for s in stocks}
            hits = [v for c, v in pool.get('items', {}).items() if c in codes] if pool['status'] == 'ok' else None
            group.update(member_count=len(stocks), limit_count=len(hits) if hits is not None else None,
                         max_streak=max((v['streak'] for v in hits), default=0) if hits is not None else None,
                         _stocks=stocks, _codes=codes)
            group['funds'] = merge_history(group['funds'], self.funds.sector_history(group['sector_code']))
            score_sector(group, scales)
            return group
        groups = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(enrich, g): g['name'] for g in shortlist}
            for future in as_completed(futures):
                try:
                    groups.append(future.result())
                except Exception as exc:
                    errors.append(f'{futures[future]}：{exc}')
        groups.sort(key=lambda g: (-g['strength'], -g['funds']['ratio'], g['sector_code']))
        chosen = []
        for group in groups:
            # Parent and child industry nodes can coexist; suppress highly overlapping entries.
            if any(len(group['_codes'] & g['_codes'])/min(len(group['_codes']),len(g['_codes'])) >= .7 for g in chosen):
                continue
            chosen.append(group)
        if not chosen:
            raise ValueError('候选成分股均不完整，不能生成可靠主线')
        chosen = board_hierarchy(chosen)
        def roles(group):
            stocks = group.pop('_stocks')
            group.pop('_codes')
            for s in stocks:
                s['board'] = group['name']
            trend_shortlist = sorted([s for s in stocks if s['pct'] > 0 and (s['funds'].get('net') or 0) > 0
                                      and (s['funds'].get('ratio') or 0) > 0 and (s.get('volume_ratio') or 0) >= 1],
                                     key=lambda s: -(s.get('amount') or 0))[:TREND_CHECK_LIMIT]
            def confirm_history(s):
                s['funds'] = merge_history(s['funds'], self.funds.stock(s['code']))
            with ThreadPoolExecutor(max_workers=3) as history_pool:
                list(history_pool.map(confirm_history, trend_shortlist))
            group['leaders'], group['role_gaps'] = choose_roles(stocks, pool.get('items', {}) if pool['status']=='ok' else {}, date)
            if pool['status'] != 'ok':
                group['role_gaps'] = ['龙头：涨停池不可用或陈旧，暂时无法确认'] + [r for r in group['role_gaps'] if not r.startswith('龙头：')]
            group['coverage'] = f"完整读取 {len(stocks)} 只成分；持续资金核验候选最多 {TREND_CHECK_LIMIT} 只；龙头最多1只、趋势与补涨分别最多{ROLE_LIMIT}只，可缺位；同股按龙头、趋势、补涨优先顺序归类；涨停统计为涨停池覆盖口径"
            return group
        with ThreadPoolExecutor(max_workers=3) as executor:
            chosen = list(executor.map(roles, chosen))
        for rank, group in enumerate(chosen, 1):
            group['rank'] = rank
        if pool['status'] != 'ok':
            errors.append('涨停池不可用：涨停数量和最高连板未知，不生成涨停龙头')
        return dict(groups=chosen, errors=errors, date=date, updated_at=now_text(), demo=False,
                    source='东方财富行业、成分股与资金流', version=VERSION, weights=WEIGHTS,
                    scope=f'同日行业至少10只成分；当日正向资金或近3/5日累计流入进入观察预筛。每轮核验前8个资金候选及轮转16个行业的多日资金；完整成分最多8个，最多展示3个低重叠板块。主线可缺位。历史资金本轮及缓存已核验 {sum(bool(g.get("history")) for g in rows)}/{len(rows)} 个行业；未核验者不推定累积。当前候选 {len(universe)} 个。')


def demo_mainlines():
    groups = []
    for rank, (name, pct, members) in enumerate(DEMO_GROUPS, 1):
        stocks = []
        for code, label in members:
            frame = demo_history(code)
            last, prev = frame.iloc[-1], frame.iloc[-2]
            stocks.append(dict(code=code, name=label, board=name, price=round(last.close,2),
                               pct=round((last.close/prev.close-1)*100,2), amount=last.amount/1e8,
                               volume_ratio=round(last.volume/frame.volume.iloc[-6:-1].mean(),2),
                               quote_date='2026-11-09', funds=demo_funds(code),
                               amplitude=round((last.high-last.low)/prev.close*100,2)))
        history = demo_funds(members[-1][0])
        f = merge_history(dict(history, net=rank*1e8, ratio=6-rank), history)
        group = dict(name=name,pct=pct,date='2026-11-09',funds=f,rise_count=28-rank,fall_count=rank+2,
                     flat_count=0,breadth=85-rank,member_count=len(stocks),limit_count=rank,max_streak=4-rank)
        score_sector(group,[1e8,2e8,3e8])
        group['leaders'],group['role_gaps'] = choose_roles(stocks,{},'2026-11-09')
        group.update(rank=rank,coverage='4 只合成样本；涨停梯队为演示值')
        groups.append(group)
    groups.sort(key=lambda g:-g['strength'])
    groups = board_hierarchy(groups)
    return dict(groups=groups,errors=[],date='2026-11-09',updated_at=now_text(),demo=True,
                source='合成演示数据 · 固定样本',scope='演示资金优先评分；未满足条件的角色留空',version=VERSION,weights=WEIGHTS)
