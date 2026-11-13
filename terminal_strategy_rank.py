"""Shared ranking for full scans and execution-time candidate rechecks."""
import pandas as pd

from terminal_engine import analyze,FACTOR_VERSION
from terminal_formula import evaluate_formula,FormulaError


def strategy_rank(config,bars,code='',name=''):
    # Legacy saved strategies keep their explicit formula ordering.
    if config.get('ranking_mode','formula')=='formula':
        return dict(rank=evaluate_formula(config['ranking'],bars,code=code,name=name),ranking_mode='formula')
    if len(bars)<35:raise FormulaError('综合评分日线不足35根，暂不参与排名')
    try:result=analyze(pd.DataFrame(bars),code,name,weights=(40,30,30),score_only=True)
    except (ValueError,KeyError,ArithmeticError) as exc:raise FormulaError('综合评分数据无效：'+str(exc)) from None
    if result['volume_ratio'] is None:raise FormulaError('综合评分缺少有效前5日成交量，不以默认分补齐')
    return dict(rank=result['score'],score=result['score'],scores=result['scores'],score_weights=[40,30,30],
                ranking_mode='composite',score_version=FACTOR_VERSION,score_reasons=result['reasons'])
