"""Whole-snapshot fund fallback; never mix industry taxonomies or money definitions."""
import re
import time
from io import StringIO

import pandas as pd
import requests

from terminal_engine import now_text
from terminal_quotes import failure


def value(x):
    try:
        v=float(str(x).replace('%','').replace(',','').replace('亿',''))
        return v if pd.notna(v) else None
    except (ValueError,TypeError):return None


class MarketRouter:
    def __init__(self, primary, quotes):
        self.primary,self.quotes=primary,quotes
        self.until={}

    def build(self):
        preferred=self.quotes.funds_preferred
        order=['em','dxx','ths']
        if preferred in order:
            order.remove(preferred);order.insert(0,preferred)
        errors=[]
        for source in order:
            if self.until.get(source,0)>time.monotonic():
                errors.append({'em':'东方财富','dxx':'短线侠','ths':'同花顺'}[source]+'：暂不可用，冷却中');continue
            try:
                data=self.primary.build() if source=='em' else self.dxx() if source=='dxx' else self.ths()
                data.update(funds_provider=source, source_fallback=bool(errors), source_errors=errors)
                with self.quotes.lock:self.quotes.states[('funds',source)]=dict(status='ok' if not data.get('readonly') else 'observation',at=now_text(),code='',reason='；'.join(data.get('errors',[])) or '正常')
                return data
            except Exception as exc:
                reason=str(exc)[:160] if source=='em' and str(exc).startswith('行业资金不可用') else failure(exc)
                errors.append({'em':'东方财富','dxx':'短线侠','ths':'同花顺'}[source]+'：'+reason)
                self.until[source]=time.monotonic()+120
                with self.quotes.lock:self.quotes.states[('funds',source)]=dict(status='unavailable',at=now_text(),code='',reason=reason)
        raise ValueError('行业资金不可用：'+'；'.join(errors))

    def ths(self):
        frames={}
        for period in (0,3,5):
            segment=('board/'+str(period)+'/') if period else ''
            url='https://data.10jqka.com.cn/funds/hyzjl/'+segment+'field/tradezdf/order/desc/page/1/ajax/1/free/1/'
            r=requests.get(url,timeout=(4,8),headers={'User-Agent':'Mozilla/5.0','Referer':'https://data.10jqka.com.cn/funds/hyzjl/'})
            r.raise_for_status()
            tables=pd.read_html(StringIO(r.text))
            frame=tables[0]
            if not {'行业','净额'}.issubset(frame.columns):raise ValueError('资金表结构改变')
            frames[period]={str(row['行业']):row for _,row in frame.iterrows()}
        groups=[]
        for name,row in frames[0].items():
            net=value(row['净额'])
            if net is None:continue
            f=dict(status='unverified',label='资金净额（非已核验主力口径）',net=net*1e8,ratio=None,date=None,positive_days=None,
                   source='同花顺行业资金公开页',period='源页面未提供可核验交易日期',retrieved_at=now_text(),
                   three_day_net=value(frames[3][name]['净额'])*1e8 if name in frames[3] and value(frames[3][name]['净额']) is not None else None,
                   five_day_net=value(frames[5][name]['净额'])*1e8 if name in frames[5] and value(frames[5][name]['净额']) is not None else None)
            groups.append(dict(name=name,pct=value(row.get('行业-涨跌幅',row.get('行业涨跌幅'))),date=None,funds=f,
                member_count=value(row.get('公司家数')),rise_count=None,breadth=None,limit_count=None,max_streak=None,
                strength=None,score_parts={},score_coverage=0,board_type='watch',board_label='备用资金观察',
                board_reason='同花顺资金口径及交易日期未核验，不沿用东方财富评分或主线结论',stage='备用源 · 仅供观察',leaders=[],
                role_gaps=['龙头：备用资金日期与口径未核验','趋势龙：备用资金日期与口径未核验','补涨：备用资金日期与口径未核验'],
                coverage='仅当前返回页样本；缺失3/5日匹配项显示未确认，不代表全市场完整排名'))
        if not groups:raise ValueError('资金表为空')
        groups.sort(key=lambda g:-g['funds']['net'])
        return dict(groups=groups[:3],readonly=True,errors=['已切换同花顺备用资金观察；日期及主力口径未核验，不用于模拟新买单'],date=None,
            updated_at=now_text(),source='同花顺行业资金公开页（独立分类、独立口径）',scope='即时/3日/5日各当前页样本；日期未核验，抓取时间不代表行情时间',version='ths-observation-v1')

    def dxx(self):
        r=requests.get('https://ds.duanxianxia.com/vendor/stockdata/platelive_1.json',timeout=(4,8))
        r.raise_for_status()
        return parse_dxx(r.json())


def parse_dxx(payload):
    from datetime import datetime
    from terminal_engine import TZ
    from terminal_funds import number
    if not isinstance(payload,dict):raise ValueError('资金数据结构无效')
    date=payload.get('date')
    parsed=datetime.strptime(date,'%Y-%m-%d').date()
    today=datetime.now(TZ).date()
    if parsed>today or (today-parsed).days>7:raise ValueError('资金文件日期无效或过旧')
    rows=payload.get('plates')
    if not isinstance(rows,list) or not rows:raise ValueError('板块资金为空')
    groups=[];seen={}
    for row in rows:
        if not isinstance(row,list) or len(row)<6:raise ValueError('板块资金列不完整')
        code,name,strength,net,limits,pct=row[:6]
        if not re.fullmatch(r'\d{6}',str(code)) or not isinstance(name,str):raise ValueError('板块代码无效')
        if code in seen:
            if seen[code][2:]!=row[2:]:raise ValueError('同一板块代码存在冲突数据')
            continue
        seen[code]=row;net=number(net);pct=number(pct)
        if net is None or pct is None:raise ValueError('资金或涨幅字段无效')
        count=number(limits)
        if count is not None and (count<0 or int(count)!=count):raise ValueError('涨停家数无效')
        f=dict(status='ok',label='主力净额（短线侠口径）',net=net,ratio=None,date=date,as_of=None,
            positive_days=None,three_day_net=None,five_day_net=None,source='短线侠板块资金',period='源文件所示交易日累计；未提供精确更新时间',retrieved_at=now_text())
        groups.append(dict(sector_code=str(code),name=name,pct=pct,date=date,funds=f,
            provider_strength=number(strength),strength=None,score_parts={},score_coverage=0,member_count=None,rise_count=None,breadth=None,
            limit_count=int(count) if count is not None else None,max_streak=None,board_type='watch',board_label='资金流入观察' if net>0 else '资金流出观察',stage='短线侠资金 · 多日待核验',
            board_reason='板块资金来自短线侠独立分类；净占比、上涨宽度及多日累计未取得，不沿用东方财富综合分或确认主线',leaders=[],
            role_gaps=['龙头：备用源未提供完整成分及个股资金','趋势龙：个股资金持续性未取得','补涨：关联标的及完整成分未取得'],
            coverage='校验源文件日期及板块代码，金额单位元；短线侠源强度不等同本工具综合评分。主力净额按原源字段展示，未证明与东方财富计算口径相同。'))
    # Prefer inflow candidates; do not erase outflow evidence from the source snapshot.
    positive=sorted([g for g in groups if g['funds']['net']>0],key=lambda g:-g['funds']['net'])
    selected=positive[:3] if positive else sorted(groups,key=lambda g:g['funds']['net'])[:3]
    return dict(groups=selected,flow_universe=[dict(code=g['sector_code'],name=g['name'],net=g['funds']['net'],pct=g['pct'],limit_count=g['limit_count']) for g in groups],
        readonly=True,errors=['已启用短线侠备用资金：可查看当日资金流向；3/5日、净占比及个股成分尚未接通，不据此自动开仓'],date=date,updated_at=now_text(),
        source='短线侠公开板块资金文件',scope=f'读取源文件{len(rows)}行，按代码去重为{len(groups)}个板块；分类包含题材/地域等重叠项，不可将净额相加当作全市场净流入。展示净流入靠前最多3个，仅为观察，不强认主线。',version='dxx-flow-v1')
