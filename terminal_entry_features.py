"""Pure, causal volume-price features and frozen-model prediction shared by research and forward simulation."""
import math
from collections import defaultdict
import numpy as np


def entry_features(bars, market=None):
    """Input must end at the decision time; no future labels enter this function."""
    if len(bars)<35:raise ValueError('Entry history shorter than 35 bars')
    close=np.array([b['close'] for b in bars],float);volume=np.array([b['volume'] for b in bars],float)
    high=np.array([b['high'] for b in bars],float);low=np.array([b['low'] for b in bars],float)
    price=close[-1];previous=close[-2];opening=float(bars[-1]['open']);width=high[-1]-low[-1]
    f=dict(price_1430=price,change_today=(price/previous-1)*100,gap_open=(opening/previous-1)*100,
           amplitude=(high[-1]-low[-1])/previous*100,
           close_location=(price-low[-1])/width if width else .5,
           upper_shadow=(high[-1]-max(opening,price))/width if width else 0.,
           lower_shadow=(min(opening,price)-low[-1])/width if width else 0.,
           body=(price-opening)/width if width else 0.)
    for n in (3,5,10,20):f['change_'+str(n)]=(price/close[-n-1]-1)*100
    for n in (5,10,20):f['ma_deviation_'+str(n)]=(price/close[-n:].mean()-1)*100
    f['volume_ratio']=volume[-1]/volume[-6:-1].mean() if volume[-6:-1].mean()>0 else None
    f['volume_5_over_20']=volume[-6:-1].mean()/volume[-21:-1].mean() if volume[-21:-1].mean()>0 else None
    f['volatility_20']=float(np.std(np.diff(np.log(close[-21:-1]))))*100
    f['ma20_slope_5']=(close[-20:].mean()/close[-25:-5].mean()-1)*100
    for n in (20,60):
        if len(bars)>=n+1:
            h,l=max(high[-n-1:-1]),min(low[-n-1:-1]);f['distance_high_'+str(n)]=(price/h-1)*100
            f['range_position_'+str(n)]=(price-l)/(h-l)*100 if h>l else 50.
        else:f['distance_high_'+str(n)]=f['range_position_'+str(n)]=None
    if market:
        f.update(market_change=market.get('change_today'),market_change_5=market.get('change_5'))
        f['relative_change_5']=f['change_5']-market['change_5'] if market.get('change_5') is not None else None
    return {key:float(value) if value is not None and math.isfinite(value) else None for key,value in f.items()}


def volume_price_features(daily,intraday,day):
    raw={r[0]:r for r in daily['raw']};qfq={r[0]:r for r in daily['qfq']}
    grouped=defaultdict(list)
    for row in intraday:grouped[row[0][:8]].append(row)
    times=['1000','1030','1100','1130','1330','1400','1430']
    def early(d):
        rows=[r for r in grouped[d.replace('-','')] if r[0][8:]<='1430']
        return rows if [r[0][8:] for r in rows]==times else []
    today=early(day)
    if not today:raise ValueError('当天14:30量价数据不完整')
    prior=sorted(d for d in raw if d<day);vol=sum(float(r[5]) for r in today)
    f={}
    for n in (5,20):
        history=[early(d) for d in prior[-n:]]
        if len(history)!=n or not all(history):
            f['same_time_volume_ratio_'+str(n)]=None
            if n==20:f['same_time_volume_percentile_20']=None
            continue
        values=[sum(float(r[5]) for r in rows) for rows in history]
        avg=float(np.mean(values));f['same_time_volume_ratio_'+str(n)]=vol/avg if avg>0 else None
        if n==20:f['same_time_volume_percentile_20']=sum(v<=vol for v in values)/n*100
    past=[float(raw[d][5]) for d in prior]
    f['prior_volume_ratio']=past[-1]/float(np.mean(past[-6:-1])) if len(past)>=6 and np.mean(past[-6:-1])>0 else None
    f['volume_contraction_3']=float(np.mean(past[-3:])/np.mean(past[-20:])) if len(past)>=20 and np.mean(past[-20:])>0 else None
    f['historical_volume_trend_5']=float(np.mean(past[-5:])/np.mean(past[-10:-5])-1)*100 if len(past)>=10 and np.mean(past[-10:-5])>0 else None
    days=[d for d in prior if d in qfq]
    if len(days)>=6:
        signed=sum(np.sign(float(qfq[d][2])-float(qfq[days[i-1]][2]))*float(raw[d][5]) for i,d in enumerate(days) if i>=len(days)-5)
        total=sum(float(raw[d][5]) for d in days[-5:])
        f['signed_volume_balance_5']=float(signed/total) if total>0 else None
    else:f['signed_volume_balance_5']=None
    recent=days[-11:]
    if len(recent)==11 and all(float(raw[d][5])>0 for d in recent):
        returns=np.diff(np.log([float(qfq[d][2]) for d in recent]));volume_changes=np.diff(np.log([float(raw[d][5]) for d in recent]))
        a,b=returns-returns.mean(),volume_changes-volume_changes.mean()
        denominator=math.sqrt(float(np.sum(a*a)*np.sum(b*b)))
        f['price_volume_correlation_10']=float(np.sum(a*b)/denominator) if denominator>1e-12 else 0.
    else:f['price_volume_correlation_10']=None
    v=float(np.mean([float(r[5]) for r in today[-3:-1]]));last=today[-1]
    acceleration=float(last[5])/v if v>0 else None
    change=(float(last[2])/float(today[-2][2])-1)*100
    f['last30_volume_acceleration']=acceleration;f['last30_price_change']=change
    f['last30_price_volume_strength']=change*math.log1p(acceleration) if acceleration is not None else None
    f['afternoon_volume_share']=sum(float(r[5]) for r in today[-3:])/vol if vol>0 else None
    f['last30_close_location']=(float(last[2])-float(last[4]))/(float(last[3])-float(last[4])) if float(last[3])>float(last[4]) else .5
    return {k:float(v) if v is not None and math.isfinite(v) else None for k,v in f.items()}


def finite(value):return value is not None and math.isfinite(float(value))


def predict_entry(model,features):
    values=[1.]
    for item in model['features']:
        value=features.get(item['feature'])
        if not finite(value):return None
        value=min(item['upper'],max(item['lower'],float(value)))
        values.append((value-item['center'])/item['scale'])
    v=np.array(values);net=float(np.sum(v*np.array(model['return_coefficients'])));z=float(np.sum(v*np.array(model['stop_coefficients'])))
    rise=float(np.sum(v*np.array(model['rise_coefficients'])))
    return dict(expected_net_pct=net,stop_probability=1/(1+math.exp(-max(-30,min(30,z)))),
                rise5_probability=1/(1+math.exp(-max(-30,min(30,rise)))))
