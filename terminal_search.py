"""Stock-name suggestions without evaluating remote JavaScript."""
import json
import re
import threading
import time

import requests

from terminal_engine import DEMO_SCAN_GROUPS, validate_code


def parse_suggestions(text):
    match = re.fullmatch(r'\s*v_hint\s*=\s*("(?:[^"\\]|\\.)*")\s*;?\s*', text)
    if not match:
        raise ValueError('搜索数据格式异常')
    decoded = json.loads(match.group(1))
    result, seen = [], set()
    for raw in decoded.split('^'):
        fields = raw.split('~')
        if len(fields) < 5 or fields[0] not in ('sh', 'sz', 'bj') or fields[4] != 'GP-A':
            continue
        code, name = fields[1], fields[2].strip()
        try:
            validate_code(code)
        except ValueError:
            continue
        if not name or code in seen:
            continue
        seen.add(code)
        result.append(dict(code=code, name=name, board='', exchange=fields[0], initials=fields[3]))
    return result


class StockSearch:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def search(self, query, demo=False):
        query = query.strip()
        if not query:
            return []
        if len(query) > 40 or not re.fullmatch(r'[\w\s*·\-\.]+', query):
            raise ValueError('请输入股票简称、代码或拼音首字母（最多 40 字）')
        if demo:
            pool = [dict(code=code, name=name, board=board, exchange='demo')
                    for board, _, members in DEMO_SCAN_GROUPS for code, name in members]
            return [s for s in pool if query.casefold() in (s['code'] + s['name']).casefold()][:10]
        with self.lock:
            entry = self.cache.get(query.casefold())
        if entry and time.monotonic() - entry[0] < 300:
            return entry[1]
        response = requests.get('https://smartbox.gtimg.cn/s3/', params={'q': query, 't': 'all'}, timeout=5)
        response.raise_for_status()
        result = parse_suggestions(response.content.decode('gbk'))
        result.sort(key=lambda s: (s['name'].casefold() != query.casefold(), s['code'] != query))
        with self.lock:
            if len(self.cache) >= 256:
                self.cache.pop(next(iter(self.cache)))
            self.cache[query.casefold()] = (time.monotonic(), result)
        return result
