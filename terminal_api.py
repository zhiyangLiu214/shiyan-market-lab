"""Local terminal API. Live failures are explicit; demo mode is opt-in."""
from __future__ import annotations

import threading
import os
from pathlib import Path
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import akshare as ak
import requests
from flask import Blueprint, jsonify, render_template, request
from terminal_funds import FundFlowService, demo_funds, missing
from terminal_paper import PaperLedger, clock as paper_clock
from terminal_paper_runner import PaperRunner
from terminal_market_cache import MarketCache
from terminal_quotes import QuoteRouter
from terminal_source_market import MarketRouter
from terminal_notes import NoteStore, NoteConflict
from terminal_search import StockSearch
from terminal_mainline import MainlineService, demo_mainlines

from terminal_engine import (DEMO_GROUPS, DEMO_SCAN_GROUPS, TZ, analyze, demo_history,
                             demo_minutes, intraday_analysis, normalize_weights, now_text,
                             qualifies_momentum, scan_options, select_mainline_roles, validate_code)


def register_terminal(app, load_boards, load_board_stocks):
    bp = Blueprint('terminal', __name__)
    lock = threading.Lock()
    history_cache, markets, jobs, minute_cache = {}, {}, {}, {}
    name_cache = {}
    quotes=QuoteRouter(None if os.environ.get('TDX_NO_PAPER_WORKER') else Path(app.root_path)/'.runtime'/'market-sources.json')
    funds = FundFlowService(None if os.environ.get('TDX_NO_PAPER_WORKER') else Path(app.root_path)/'.runtime'/'fund-source-health.json',quotes.tushare)
    stock_search = StockSearch()
    mainline = MainlineService(funds)
    fund_router=MarketRouter(mainline,quotes)

    def history(code):
        revision=quotes.revision;key=(code,revision)
        with lock:
            entry = history_cache.get(key)
        if entry and time.time() - entry[0] < 25:
            return entry[1].copy()
        data=quotes.fetch('daily',code)
        with lock:
            if len(history_cache) > 256:
                history_cache.clear()
            history_cache[key] = (time.time(), data)
        return data

    def build_market(demo):
        if demo:return demo_mainlines()
        revision=quotes.revision
        result=fund_router.build();result['source_revision']=revision
        return result

    market_cache=MarketCache(build_market,
                             None if os.environ.get('TDX_NO_PAPER_WORKER') else Path(app.root_path)/'.runtime')

    def market(demo):
        result=market_cache.get(demo)
        if not demo and not result.get('loading') and result.get('source_revision',0)!=quotes.revision:
            result.update(stale=True,last_error='来源配置已变更，等待新来源数据；旧快照不用于新买单')
        return result

    def get_detail(code, demo, name='', board='', weights=(.4, .3, .3)):
        validate_code(code)
        if demo:
            for group_name, _, members in DEMO_GROUPS:
                for item_code, item_name in members:
                    if code == item_code:
                        name, board = item_name, group_name
            name = name or f'示例·{code}'
        frame = demo_history(code) if demo else history(code)
        if not demo and (not name or name == code):
            with lock:
                cached_name = name_cache.get(code)
            if cached_name and time.time() - cached_name[0] < 86400:
                name, cached_board = cached_name[1:]
                board = board or cached_board
        if not demo and (not name or name == code):
            try:
                info = ak.stock_individual_info_em(symbol=code, timeout=5)
                fields = dict(zip(info['item'], info['value']))
                name = str(fields.get('股票简称') or code)
                board = board or str(fields.get('行业') or '')
            except Exception:
                pass  # Metadata failure must not discard valid daily bars.
            if not name or name == code:
                try:
                    exchange = 'sh' if code.startswith('6') else 'bj' if code[0] in '489' else 'sz'
                    response = requests.get(f'https://qt.gtimg.cn/q={exchange}{code}', timeout=5)
                    response.raise_for_status()
                    fields = response.content.decode('gbk').split('~')
                    # Verify the returned symbol before accepting a fallback name.
                    if len(fields) > 2 and fields[2] == code and fields[1].strip():
                        name = fields[1].strip()
                except Exception:
                    pass
            if name and name != code:
                with lock:
                    name_cache[code] = (time.time(), name, board)
        result=analyze(frame, code, name[:40], board[:40], weights, demo)
        if not demo:result.update(frame.attrs)
        return result

    def run_scan(job_id, demo, weights, options):
        """Independent board universe and momentum gate; never call market()."""
        try:
            if demo:
                universe = [{'name': name, 'pct': pct, 'members': members}
                            for name, pct, members in DEMO_SCAN_GROUPS if pct >= options['min_board_pct']]
            else:
                boards = load_boards()
                universe = [{'name': str(row['name']), 'pct': float(row['pct'])}
                            for _, row in boards[boards['pct'] >= options['min_board_pct']].head(8).iterrows()]
            with lock:
                jobs[job_id]['universe_count'] = len(universe)
                jobs[job_id]['reason'] = ('当前数据源没有涨幅达到 '+str(options['min_board_pct'])+'% 的板块' if not universe else '继续核验板块内个股动量及85分门槛')
                jobs[job_id]['phase'] = '读取板块成分'
            candidates, checks = {}, {}
            for sector in universe:
                name = sector['name']
                try:
                    members = ([{'code': code, 'name': stock_name, 'board': name}
                                for code, stock_name in sector['members']] if demo else
                               [{**row, 'board': name} for row in load_board_stocks(name).head(20).to_dict('records')])
                    checks[name] = {'name': name, 'pct': sector['pct'], 'sampled': len(members),
                                    'analyzed': 0, 'qualified': 0, 'errors': 0, 'passed': False,
                                    'candidates': []}
                    for member in members:
                        candidates[(name, member['code'])] = member
                except Exception:
                    with lock:
                        jobs[job_id]['errors'].append(f'{name}：成分股读取失败')
            with lock:
                jobs[job_id]['total'] = len(candidates)
                jobs[job_id]['phase'] = '检验基础动量'
                jobs[job_id]['board_checks'] = [{k: v for k, v in c.items() if k != 'candidates'} for c in checks.values()]
            with ThreadPoolExecutor(max_workers=4) as pool:
                pending = {pool.submit(get_detail, item['code'], demo, item['name'], item['board'], weights): item
                           for item in candidates.values()}
                for future in as_completed(pending):
                    stock = pending[future]
                    try:
                        detail = future.result()
                        detail.pop('bars', None)
                        with lock:
                            check = checks[stock['board']]
                            check['analyzed'] += 1
                            if qualifies_momentum(detail):
                                check['qualified'] += 1
                                check['candidates'].append(detail)
                    except Exception:
                        with lock:
                            checks[stock['board']]['errors'] += 1
                    with lock:
                        jobs[job_id]['completed'] += 1
                        for check in checks.values():
                            check['passed'] = check['qualified'] >= options['min_companies']
                        jobs[job_id]['board_checks'] = [{k: v for k, v in c.items() if k != 'candidates'} for c in checks.values()]
                        # Expose only candidates of sectors that already meet the gate.
                        jobs[job_id]['results'] = sorted(
                            [row for check in checks.values() if check['passed'] for row in check['candidates']
                             if row['score'] >= 85],
                            key=lambda row: (-row['score'], -row['pct'], row['code']))
            with lock:
                for check in checks.values():
                    check.pop('candidates', None)
                    check['status'] = ('通过' if check['passed'] else
                                       '数据不足' if check['errors'] else '样本内未达标')
                jobs[job_id]['board_checks'] = list(checks.values())
                if universe:
                    jobs[job_id]['reason'] = f"涨幅达标{len(universe)}个板块，核验{len(candidates)}只个股；{sum(c['passed'] for c in checks.values())}个板块通过多股动量门槛，最终85分以上{len(jobs[job_id]['results'])}只"
                    jobs[job_id]['reason'] += '；' + '；'.join(
                        f"{c['name']}：{c['qualified']}/{options['min_companies']}家基础动量达标"
                        + (f"，{c['errors']}只数据不可用" if c['errors'] else '')
                        for c in checks.values())
                jobs[job_id]['status'] = 'complete'
                jobs[job_id]['phase'] = '扫描完成'
                jobs[job_id]['finished_at'] = now_text()
                if not candidates and universe:
                    jobs[job_id]['status'] = 'failed'
                    jobs[job_id]['errors'].append('成分股均不可用，未生成三维排名')
                jobs[job_id]['errors'].extend(
                    f"{c['name']}：{c['errors']} 只日线不可用，未计入达标数" for c in checks.values() if c['errors'])
        except Exception:
            with lock:
                jobs[job_id]['status'] = 'failed'
                jobs[job_id]['errors'].append('数据源连接失败，请重试或切换演示模式')

    def get_minute(code, demo):
        validate_code(code)
        key = (code, demo, quotes.revision)
        with lock:
            entry = minute_cache.get(key)
        if entry and time.time() - entry[0] < 25:
            return entry[1]
        detail=intraday_analysis(demo_minutes(code),code,True) if demo else quotes.fetch('minute',code)
        with lock:
            if len(minute_cache) > 128:
                minute_cache.clear()
            minute_cache[key] = (time.time(), detail)
        return detail

    @bp.get('/api/terminal/intraday/<code>')
    def intraday_api(code):
        try:
            return jsonify(ok=True, data=get_minute(code, request.args.get('mode') == 'demo'))
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception:
            return jsonify(ok=False, error='分时数据暂不可用，请稍后重试；未使用日线代替'), 503

    @bp.get('/')
    def home():
        return render_template('terminal.html')

    @bp.get('/api/terminal/sources')
    def source_status():
        return jsonify(ok=True,data=dict(quotes.snapshot(), stock_funds_health=funds.health()))

    @bp.post('/api/terminal/sources')
    def source_config():
        body=request.get_json(silent=True) or {}
        try:
            result=quotes.configure(body.get('preferred','auto'),body.get('funds_preferred','auto'))
            with lock:
                history_cache.clear();minute_cache.clear()
            fund_router.until.clear()
            with market_cache.lock:
                for saved in market_cache.states.values():saved.update(stamp=None,attempt=None)
            paper.last_pool.clear();paper.cache.clear();paper.wakeup.set()
            return jsonify(ok=True,data=result)
        except ValueError as exc:return jsonify(ok=False,error=str(exc)),400

    @bp.get('/api/terminal/market')
    def market_api():
        try:
            return jsonify(ok=True, data=market(request.args.get('mode') == 'demo'))
        except Exception:
            return jsonify(ok=False, error='行情源暂不可用。请检查网络后重试，也可主动切换演示模式。'), 503

    @bp.get('/api/terminal/stock/<code>')
    def stock_api(code):
        try:
            validate_code(code)
            weights = normalize_weights([request.args.get(k, d) for k, d in zip(('w1', 'w2', 'w3'), (40, 30, 30))])
            result = get_detail(code, request.args.get('mode') == 'demo', request.args.get('name', ''),
                                request.args.get('board', ''), weights)
            result['funds'] = demo_funds(code) if result['demo'] else funds.stock(code)
            return jsonify(ok=True, data=result)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception:
            return jsonify(ok=False, error='个股日线获取失败，请稍后重试'), 503

    @bp.get('/api/terminal/search')
    def search_api():
        try:
            demo = request.args.get('mode') == 'demo'
            items = stock_search.search(request.args.get('q', ''), demo)
            return jsonify(ok=True, data={'items': items, 'source': '合成演示样本' if demo else '腾讯股票检索'})
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception:
            return jsonify(ok=False, error='名称搜索暂不可用，可稍后重试或直接输入六位股票代码'), 503

    @bp.post('/api/terminal/scan')
    def scan_api():
        body = request.get_json(silent=True) or {}
        try:
            weights = normalize_weights(body.get('weights', [40, 30, 30]))
            options = scan_options(body)
        except (ValueError, TypeError):
            return jsonify(ok=False, error='请检查权重、板块涨幅门槛和达标家数（5～8 家）'), 400
        demo = body.get('mode') == 'demo'
        with lock:
            # Bound background work; reuse the same running job, reject conflicting scans.
            for job in jobs.values():
                if job['status'] == 'running':
                    if job['demo'] == demo and job['weights'] == weights and job['options'] == options:
                        return jsonify(ok=True, data={'id': job['id']})
                    return jsonify(ok=False, error='已有扫描进行中，请完成后再调整参数'), 409
            while len(jobs) >= 12:
                jobs.pop(next(iter(jobs)))
            job_id = uuid.uuid4().hex
            jobs[job_id] = {'id': job_id, 'status': 'running', 'demo': demo, 'weights': weights,
                            'options': options, 'min_score': 85, 'board_checks': [], 'phase': '准备独立板块扫描',
                            'scope': '涨幅达标行业中前 8 个，每行业最多 20 只涨幅靠前成分；非全板块或全市场',
                            'total': 0, 'completed': 0, 'results': [], 'errors': [], 'started_at': now_text()}
        threading.Thread(target=run_scan, args=(job_id, demo, weights, options), daemon=True).start()
        return jsonify(ok=True, data={'id': job_id}), 202

    @bp.get('/api/terminal/scan/<job_id>')
    def scan_status(job_id):
        with lock:
            job = jobs.get(job_id)
            if job is None:
                return jsonify(ok=False, error='扫描记录已过期，请重新扫描'), 404
            return jsonify(ok=True, data=job)

    def paper_detail(code, demo, name='', board=''):
        data = get_detail(code, demo, name, board)
        data['funds'] = demo_funds(code) if demo else funds.stock(code)
        return data

    def paper_scan(demo):
        weights, options = normalize_weights([40,30,30]), scan_options({})
        with lock:
            matches = [j for j in jobs.values() if j['demo']==demo and j['weights']==weights and j['options']==options]
            running = any(j['status']=='running' for j in jobs.values())
            complete = [j for j in matches if j['status']=='complete']
            latest = max(complete, key=lambda j:j.get('finished_at','')) if complete else None
            fresh = latest and (datetime.now(TZ).replace(tzinfo=None)-datetime.strptime(latest['finished_at'],'%Y-%m-%d %H:%M:%S')).total_seconds() < 120
            if not running and (not latest or not fresh):
                while len(jobs)>=12:
                    expired = next((k for k,j in jobs.items() if j['status']!='running'), None)
                    if expired is None: break
                    jobs.pop(expired)
                job_id=uuid.uuid4().hex
                jobs[job_id]={'id':job_id,'status':'running','demo':demo,'weights':weights,'options':options,
                             'min_score':85,'board_checks':[],'phase':'模拟账户：独立三维扫描',
                             'scope':'前8个达标板块，每板块最多20只，非全市场','total':0,'completed':0,
                             'results':[],'errors':[],'started_at':now_text()}
                threading.Thread(target=run_scan,args=(job_id,demo,weights,options),daemon=True).start()
            return {'items':list(latest['results']),'as_of':latest['finished_at'],'complete':not latest.get('errors'),'reason':'；'.join(latest.get('errors',[])) or latest.get('reason','本轮暂无85分以上候选')} if fresh else None

    paper_ledger = PaperLedger(Path(app.root_path)/'.runtime'/'paper-trading.sqlite3')
    paper = PaperRunner(paper_ledger,market,paper_scan,paper_detail,get_minute)

    @bp.get('/api/terminal/paper')
    def paper_state():
        mode='demo' if request.args.get('mode')=='demo' else 'live'
        if not os.environ.get('TDX_NO_PAPER_WORKER'): paper.ensure()
        return jsonify(ok=True,data=paper_ledger.snapshot(mode))

    @bp.post('/api/terminal/paper')
    def paper_action():
        body=request.get_json(silent=True) or {}
        mode='demo' if body.get('mode')=='demo' else 'live'
        try:
            action=body.get('action')
            if action=='start': paper_ledger.start(mode)
            elif action=='toggle':
                if not isinstance(body.get('enabled'),bool): raise ValueError('运行状态无效')
                paper_ledger.toggle(mode,body['enabled'])
            elif action=='cap': paper_ledger.set_cap(mode,body.get('account'),body.get('cap'))
            elif action=='cancel': paper_ledger.cancel(mode,str(body.get('id','')))
            elif action=='order':
                snap=paper_ledger.snapshot(mode);account=body.get('account');code=str(body.get('code',''))
                pool=snap['candidates'].get(account,{})
                meta=next((s for s in pool.get('items',[]) if s['code']==code),None)
                position=next((p for a in snap['accounts'] if a['id']==account for p in a['positions'] if p['code']==code),None)
                if body.get('side')=='buy' and (not meta or not pool.get('at') or (datetime.now(TZ).replace(tzinfo=None)-datetime.strptime(pool['at'],'%Y-%m-%d %H:%M:%S')).total_seconds()>180):
                    raise ValueError('买入仅限本账户最新筛选池，等待本轮筛选完成')
                if body.get('side')=='sell' and not position: raise ValueError('该账户没有此持仓')
                quote=paper.read(code,mode,meta or position)
                if not quote['valid']: raise ValueError('当前分钟行情不可用于模拟交易')
                daily=quote['daily'];plan=position['plan'] if position else dict(daily['plan'],support=daily.get('support'))
                paper_ledger.submit(mode,account,code,daily['name'],body.get('side'),body.get('qty'),quote['price'],quote['as_of'],plan,str(body.get('reason') or '手动模拟操作'),'manual')
            else: raise ValueError('未知模拟操作')
            if not os.environ.get('TDX_NO_PAPER_WORKER'):
                paper.ensure()
                paper.wakeup.set()
            return jsonify(ok=True,data=paper_ledger.snapshot(mode))
        except (ValueError,TypeError,KeyError) as exc:
            return jsonify(ok=False,error=str(exc)),400
        except Exception:
            return jsonify(ok=False,error='模拟操作未完成，账本事务已回滚，请刷新核对'),503

    notes = NoteStore(Path(app.root_path)/'.runtime'/'yeren-notes.sqlite3',
                      Path(app.root_path)/'research'/'yeren_tech_catalog.json')

    @bp.get('/api/terminal/notes')
    def notes_list():
        return jsonify(ok=True,data=notes.list())

    @bp.post('/api/terminal/notes')
    def notes_action():
        body=request.get_json(silent=True)
        if not isinstance(body,dict): return jsonify(ok=False,error='笔记格式无效'),400
        try:
            action=body.get('action','save')
            if action=='save': item=notes.save(body.get('note'))
            elif action in ('archive','restore'):
                note_id=body.get('id')
                if not isinstance(note_id,str): raise ValueError('笔记编号无效')
                item=notes.archive(note_id,body.get('revision'),action=='archive')
            else: raise ValueError('未知笔记操作')
            return jsonify(ok=True,data=item)
        except NoteConflict as exc: return jsonify(ok=False,error=str(exc)),409
        except (ValueError,TypeError) as exc: return jsonify(ok=False,error=str(exc)),400

    if not os.environ.get('TDX_NO_PAPER_WORKER') and any(paper_ledger.snapshot(m)['run'] for m in ('live','demo')):
        paper.ensure()
    app.register_blueprint(bp)
