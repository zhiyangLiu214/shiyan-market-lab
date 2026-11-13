"""Causal minute-close exit decisions. Trigger intent survives T+1 restrictions."""
from datetime import datetime
from terminal_paper import number,money

PATTERNS=('vwap_cross','rebound_failure','range_break','spike_reversal')


def minute_metrics(points):
    result=dict(speed=None,volume_increasing=False,price_increasing=False,below_average=False)
    if not points:return result
    last=points[-1]
    result['below_average']=bool(number(last.get('average')) and last['average']>0 and last['price']<last['average'])
    if len(points)<2:return result
    previous=points[-2]
    elapsed=(datetime.strptime(last['time'],'%H:%M')-datetime.strptime(previous['time'],'%H:%M')).total_seconds()
    if elapsed!=60 or previous['price']<=0:return result
    result.update(speed=(last['price']/previous['price']-1)*100,price_increasing=last['price']>previous['price'],
                  volume_increasing=bool(number(last.get('volume')) and number(previous.get('volume')) and last['volume']>previous['volume']))
    return result


def pattern_reasons(points,chosen):
    result=[]
    def tail(n):
        rows=points[-n:]
        if len(rows)!=n:return []
        if any((datetime.strptime(b['time'],'%H:%M')-datetime.strptime(a['time'],'%H:%M')).total_seconds()!=60 for a,b in zip(rows,rows[1:])):return []
        return rows
    last=points[-1];price=last['price'];below=number(last.get('average')) and last['average']>0 and price<last['average']
    pair=tail(2)
    if 'vwap_cross' in chosen and pair and number(pair[0].get('average')) and pair[0]['average']>0 and pair[0]['price']>=pair[0]['average'] and below:
        result.append('浮盈保护：下穿分时均价线')
    rows=tail(6)
    if 'rebound_failure' in chosen and rows:
        prev=rows[-2];weak=sum(number(p.get('average')) and p['price']<p['average'] for p in rows[:-1])>=3
        near=number(prev.get('average')) and prev['average']>0 and abs(prev['price']/prev['average']-1)<=.002
        if weak and near and price<prev['price'] and below:result.append('浮盈保护：反弹接近均价线后再次回落')
    rows=tail(11)
    if 'range_break' in chosen and rows:
        prices=[p['price'] for p in rows[:-1]]
        if (max(prices)/min(prices)-1)*100<=1 and price<min(prices):result.append('浮盈保护：跌破前10根分钟收盘整理区低点')
    rows=tail(6)
    if 'spike_reversal' in chosen and rows:
        prices=[p['price'] for p in rows[:-1]]
        if (max(prices)/prices[0]-1)*100>=2 and (price/max(prices)-1)*100<=-.5 and below:
            result.append('浮盈保护：急涨后回落并跌破均价线')
    return result


def exit_decision(position,q,cfg,lines,state=None):
    state=dict(state or {});notices=[];points=q['points'];price=q['price'];day=q['date']
    metrics=minute_metrics(points)
    if state.get('exit_reason'):return dict(sell=True,reason=state['exit_reason'],state=state,lines=lines,notices=[],metrics=metrics)
    limit=.2 if position['code'].startswith(('30','688')) else .1
    upper=money(q['prev_close']*(1+limit)) if number(q.get('prev_close')) else None
    opened=position['plan'].get('_opened_at','9999')
    relevant=[p for p in points if day+' '+p['time']+':00'>=opened]
    touched=bool(upper and any(abs(p['price']-upper)<.005 for p in relevant))
    if touched:
        if state.get('limit_day')!=day:
            notices.append(dict(kind='limit_up',text='已观察到涨停，暂不止盈；止损保护线抬至涨停价，止盈同步上移'))
        state.update(limit_day=day,limit_price=upper)
        if money(upper*(1+cfg['take_profit']/100))>lines['target']:state['profit_armed']=False
        lines=dict(lines,stop=max(lines['stop'],upper),target=max(lines['target'],money(upper*(1+cfg['take_profit']/100))))
    sealed=bool(upper and abs(price-upper)<.005)
    reason=None
    if state.get('limit_day')==day and not sealed:
        reason='涨停后炸板，触发卖出';notices.append(dict(kind='break_limit',text=reason))
    elif price<lines['stop']-.005:reason='跌破止损/涨停保护线，触发卖出'
    elif sealed:
        return dict(sell=False,reason='涨停中暂不止盈，继续保护与观察',state=state,lines=lines,notices=notices,metrics=metrics)
    elif price<=lines['stop']:reason='跌破止损/涨停保护线，触发卖出'
    else:
        state['profit_armed']=bool(state.get('profit_armed') or price>=lines['target'])
        patterns=pattern_reasons(points,cfg.get('exit_patterns',[])) if price>position['average_cost'] else []
        if patterns:reason='；'.join(patterns)
        if state['profit_armed'] and not reason:
            if metrics['below_average']:reason='止盈观察后跌破分时均价线，触发卖出'
            elif metrics['speed'] is not None and metrics['speed']<0:reason='止盈观察后1分钟涨速小于0，触发卖出'
            else:
                strong=metrics['speed'] is not None and metrics['speed']>1 and metrics['volume_increasing'] and metrics['price_increasing']
                text='量增价升且1分钟涨速大于1%，暂不止盈' if strong else '已到止盈观察区，暂未出现负涨速或跌破均价的卖出触发'
                return dict(sell=False,reason=text,state=state,lines=lines,notices=notices,metrics=metrics)
    if reason:
        state['exit_reason']=reason
        if not notices:notices.append(dict(kind='exit',text=reason))
    return dict(sell=bool(reason),reason=reason or '未到退出条件，继续持有',state=state,lines=lines,notices=notices,metrics=metrics)
