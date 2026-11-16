"""Isolated AKShare calls with a hard deadline and retained source metadata."""
import contextlib
import io
import json
import sys

ALLOWED = {'stock_sector_fund_flow_rank', 'stock_sector_fund_flow_hist',
           'stock_sector_fund_flow_summary', 'stock_individual_fund_flow',
           'stock_individual_fund_flow_rank', 'stock_board_industry_name_em',
           'stock_board_industry_cons_em', 'stock_zt_pool_em'}
FIELDS = ('f12', 'f14', 'f124', 'f62', 'f184', 'f3', 'f104', 'f105', 'f106')


def execute(method, params):
    if method not in ALLOWED:
        raise ValueError('Unsupported AKShare interface')
    import akshare as ak
    import requests
    snapshots, histories, pools = {}, [], []
    original = requests.Session.request
    interface = method

    def request(session, method, url, **kwargs):
        args = dict(kwargs.get('params') or {})
        extra_stamp = interface == 'stock_board_industry_cons_em' and str(args.get('fs', '')).startswith('b:') and 'f124' not in args.get('fields', '').split(',')
        if extra_stamp:
            # AKShare drops source timestamps from its public DataFrame. Ask for
            # metadata only, then restore its expected schema before parsing.
            args['fields'] = args['fields']+',f124'
            kwargs['params'] = args
        timeout = kwargs.get('timeout')
        kwargs['timeout'] = (4, 8) if timeout is None else min(timeout, 8) if isinstance(timeout, (int, float)) else tuple(min(x or 8, 8) for x in timeout)
        response = original(session, method, url, **kwargs)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            return response
        data = payload.get('data') if isinstance(payload, dict) else None
        if isinstance(data, dict):
            diff = data.get('diff')
            if isinstance(diff, (dict, list)) and 'total' in data:
                scope = str(args.get('fs', ''))
                saved = snapshots.setdefault(scope, {'scope': scope, 'total': data['total'], 'rows': {}})
                if saved['total'] != data['total']:
                    raise ValueError('AKShare pagination total changed')
                for row in diff.values() if isinstance(diff, dict) else diff:
                    if isinstance(row, dict) and row.get('f12'):
                        saved['rows'][str(row['f12'])] = {k: row.get(k) for k in FIELDS}
                # This pinned AKShare version expects a dict in this one adapter.
                if interface == 'stock_sector_fund_flow_summary' and scope.startswith('b:') and isinstance(diff, list):
                    data['diff'] = {str(i): row for i, row in enumerate(diff)}
                    response.json = lambda **_: payload
                if extra_stamp:
                    for row in diff.values() if isinstance(diff, dict) else diff:
                        row.pop('f124', None)
                    response.json = lambda **_: payload
            if 'klines' in data:
                histories.append({'code': data.get('code'), 'requested': args.get('secid')})
            if 'pool' in data:
                pools.append({'date': str(data.get('qdate')), 'total': data.get('tc'), 'count': len(data['pool'])})
        return response

    requests.Session.request = request
    try:
        # Discard library progress output; stdout is a JSON protocol only.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            frame = getattr(ak, method)(**params)
        return {'rows': json.loads(frame.to_json(orient='records', date_format='iso')),
                'snapshots': [{**s, 'rows': list(s['rows'].values())} for s in snapshots.values()],
                'histories': histories, 'pools': pools}
    finally:
        requests.Session.request = original


if __name__ == '__main__':
    from terminal_quotes import failure
    try:
        body = json.load(sys.stdin)
        print(json.dumps({'ok': True, 'data': execute(body['method'], body.get('params', {}))}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': failure(exc)}, ensure_ascii=False))
