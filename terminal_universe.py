"""Exchange stock rosters and explicit board membership; no index/code guessing."""
from io import BytesIO
import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

BOARDS={'main':'沪深主板','chinext':'创业板','star':'科创板'}


def stock_board(code):
    if not isinstance(code,str) or not re.fullmatch(r'\d{6}',code):return None
    if code.startswith(('600','601','603','605','000','001','002','003')):return 'main'
    if code.startswith(('300','301')):return 'chinext'
    if code.startswith('688'):return 'star'
    return None


def in_boards(code,boards):return stock_board(code) in boards


class StockUniverse:
    def __init__(self,fetch=None):
        self.fetch=fetch or self._fetch;self.lock=threading.Lock();self.cached=None;self.until=0

    def load(self,boards):
        with self.lock:
            if self.cached is None or time.monotonic()>self.until:
                rows=self.fetch()
                seen={}
                for row in rows:
                    code=str(row['code']);board=stock_board(code)
                    if board:seen[code]=dict(code=code,name=row['name'],market_board=board)
                if any(not any(r['market_board']==b for r in seen.values()) for b in BOARDS):raise ValueError('交易所股票名录缺少市场，不能作为完整范围')
                self.cached=list(seen.values());self.until=time.monotonic()+1800
            rows=[dict(r) for r in self.cached if r['market_board'] in boards]
        return rows

    @staticmethod
    def _fetch():
        def sh(kind):
            r=requests.get('https://query.sse.com.cn/sseQuery/commonQuery.do',params={
                'STOCK_TYPE':kind,'sqlId':'COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L','COMPANY_STATUS':'2,4,5,7,8',
                'type':'inParams','isPagination':'true','pageHelp.pageSize':'10000','pageHelp.pageNo':'1'},
                headers={'Referer':'https://www.sse.com.cn/assortment/stock/list/share/','User-Agent':'Mozilla/5.0'},timeout=15)
            r.raise_for_status();payload=r.json();data=payload['result']
            total=payload.get('pageHelp',{}).get('total',len(data))
            if not data or int(total)!=len(data):raise ValueError('上交所名录分页不完整')
            return [dict(code=str(x['A_STOCK_CODE']),name=x['SEC_NAME_CN']) for x in data]
        def sz():
            r=requests.get('https://www.szse.cn/api/report/ShowReport',params={'SHOWTYPE':'xlsx','CATALOGID':'1110','TABKEY':'tab1'},timeout=15)
            r.raise_for_status();frame=pd.read_excel(BytesIO(r.content),engine='openpyxl',dtype=str)
            if len(frame)<10:raise ValueError('深交所名录不完整')
            return [dict(code=str(x['A股代码']).zfill(6),name=html.unescape(re.sub('<[^>]+>','',str(x['A股简称'])))) for _,x in frame.iterrows()]
        with ThreadPoolExecutor(max_workers=3) as executor:
            jobs=[executor.submit(sh,'1'),executor.submit(sh,'8'),executor.submit(sz)]
            return [r for job in jobs for r in job.result()]
