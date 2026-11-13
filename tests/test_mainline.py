import copy
import unittest
from unittest.mock import patch

from terminal_funds import FundFlowService
from terminal_mainline import MainlineService, score_sector, choose_roles, merge_history, WEIGHTS, board_hierarchy

DATE = '2026-11-10'


def group(name='行业', net=1e8, ratio=5, pct=2):
    return dict(name=name, date=DATE, pct=pct, funds=dict(status='ok', date=DATE, net=net,
                ratio=ratio, positive_days=2), breadth=80, member_count=20, limit_count=2, max_streak=2)


def stock(code, pct=5, net=1e7, ratio=5, amount=10, days=2, volume=1.5):
    return dict(code=code, name=code, price=10, pct=pct, amount=amount, volume_ratio=volume,
                funds=dict(status='ok', date=DATE, net=net, ratio=ratio, positive_days=days))


class MainlineTests(unittest.TestCase):
    def test_money_outweighs_pure_price_rank_and_gates_outflows(self):
        high_pct = group('高涨幅弱资金', net=1e7, ratio=.1, pct=8)
        funded = group('资金主线', net=2e8, ratio=9, pct=1.5)
        scales = [1e7, 2e8]
        score_sector(high_pct, scales)
        score_sector(funded, scales)
        self.assertGreater(funded['strength'], high_pct['strength'])
        self.assertEqual(sum(WEIGHTS[k] for k in ('净流入占比','净流入规模','资金持续性')),60)
        self.assertIsNone(score_sector(group(net=-1e8), scales))
        self.assertIsNone(score_sector(group(ratio=0), scales))
        stale = group(); stale['funds']['status']='stale'
        self.assertIsNone(score_sector(stale, scales))
        wrong_date = group(); wrong_date['funds']['date']='2026-11-09'
        self.assertIsNone(score_sector(wrong_date, scales))

    def test_missing_fields_are_unknown_and_coverage_is_visible(self):
        g = group(); g['limit_count']=None; g['max_streak']=None; g['funds']['positive_days']=None
        score_sector(g,[1e8,2e8])
        self.assertIsNone(g['score_parts']['资金持续性']['score'])
        self.assertIsNone(g['score_parts']['涨停扩散']['score'])
        self.assertEqual(g['score_coverage'],75)
        self.assertIn('待确认',g['stage'])

    def test_roles_require_money_pool_and_persistence(self):
        stocks = [stock('600001', pct=10, net=-1e8), stock('600002', pct=10),
                  stock('600003', amount=40, days=None), stock('600004', amount=30),
                  stock('600005', pct=3, amount=29, ratio=9, days=1), stock('600006', amount=1, volume=.5)]
        pool = {'600001':dict(streak=5,breaks=0,first_seal=92500),
                '600002':dict(streak=2,breaks=1,first_seal=100000)}
        roles, gaps = choose_roles(stocks,pool,DATE)
        self.assertEqual([r['code'] for r in roles],['600002','600004','600005','600003'])
        self.assertEqual(gaps,[])
        self.assertEqual(len({r['code'] for r in roles}),len(roles))
        self.assertNotIn('600001',[r['code'] for r in roles])

    def test_multiple_trends_and_follows_without_duplicate_symbols(self):
        stocks = [stock('600001', pct=10)]
        stocks += [stock(str(600010+i), amount=70-i*10) for i in range(3)]
        stocks += [stock(str(600020+i), pct=3, amount=35-i, days=1) for i in range(2)]
        stocks += [stock(str(600030+i), amount=1, volume=.5) for i in range(8)]
        roles, gaps = choose_roles(stocks, {'600001':dict(streak=2, breaks=0, first_seal=93000)}, DATE)
        self.assertEqual(sum(r['role']=='龙头候选' for r in roles), 1)
        self.assertEqual(sum(r['role']=='趋势龙候选' for r in roles), 3)
        self.assertEqual(sum(r['role']=='补涨观察' for r in roles), 2)
        self.assertEqual(len(roles), len({r['code'] for r in roles}))
        self.assertEqual(gaps, [])

    def test_board_hierarchy_prefers_persistence_and_does_not_force_trend(self):
        groups = [dict(group(str(i)), strength=90-i) for i in range(4)]
        groups[2]['funds']['positive_days']=None
        groups[3]['funds']['positive_days']=4
        chosen=board_hierarchy(groups)
        self.assertEqual([g['name'] for g in chosen], ['0','1','3'])
        self.assertEqual([g['board_type'] for g in chosen], ['main','secondary','trend'])
        groups[3]['funds']['positive_days']=None
        self.assertEqual(board_hierarchy(groups)[2]['board_type'],'watch')
        self.assertEqual(len(board_hierarchy(groups[:1])),1)

    def test_no_mainline_is_fabricated_from_rank_or_one_day_flow(self):
        groups = [dict(group(str(i)), strength=90-i) for i in range(3)]
        for g in groups:
            g['funds']['positive_days'] = None
        self.assertTrue(all(g['board_type']=='watch' for g in board_hierarchy(groups)))
        groups[0]['funds'].update(three_day_net=100, five_day_net=200)
        groups[0]['limit_count']=0
        self.assertEqual(board_hierarchy(groups)[0]['board_type'],'trend')
        groups[0]['funds'].update(net=-10, ratio=-1)
        self.assertIsNotNone(score_sector(groups[0], [100]))
        self.assertTrue(all(g['board_type'] not in ('main','secondary','trend') for g in board_hierarchy(groups)))
        self.assertEqual(groups[0]['board_type'],'potential')

    def test_no_forced_leader_and_stale_names_do_not_become_trends(self):
        s = stock('600001',days=None)
        roles,gaps=choose_roles([s],{},DATE)
        self.assertEqual(roles,[])
        self.assertEqual(len(gaps),3)
        s['funds']['date']='2026-11-09'
        self.assertEqual(choose_roles([s],{},DATE)[0],[])

    def test_flow_history_only_confirms_same_day_and_uses_current_snapshot(self):
        snapshot=dict(status='ok', date=DATE, net=-10,ratio=-1)
        history=dict(status='ok',date=DATE,history=[dict(date='2026-11-09',net=20),dict(date=DATE,net=10)])
        merged=merge_history(snapshot,history)
        self.assertEqual(merged['positive_days'],0)
        self.assertIsNone(merged['five_day_net'])
        history['date']='2026-11-09'
        self.assertIsNone(merge_history(snapshot,history)['positive_days'])

    def test_rolling_flows_include_current_snapshot_and_negative_days(self):
        snapshot = dict(status='ok', date=DATE, net=50, ratio=2)
        rows = [dict(date=d, net=n) for d, n in zip(
            ['2026-11-04', '2026-11-05', '2026-11-06', '2026-11-09', '2026-11-10', DATE],
            [999, 10, -20, 30, -40, 999])]
        history = dict(status='ok', date=DATE, history=rows)
        merged = merge_history(snapshot, history)
        self.assertEqual(merged['three_day_net'], 40)
        self.assertEqual(merged['five_day_net'], 30)
        self.assertEqual(rows[-1]['net'], 999)
        history['history'] = rows[-2:]
        self.assertIsNone(merge_history(snapshot, history)['three_day_net'])
        history['date'] = '2026-11-10'
        self.assertIsNone(merge_history(snapshot, history)['five_day_net'])

    def test_completed_yesterday_history_joins_today_live_snapshot_without_relabelling(self):
        snapshot=dict(status='ok',date='2026-11-13',net=50,ratio=2)
        rows=[dict(date=d,net=n) for d,n in [('2026-11-09',10),('2026-11-10',-20),('2026-11-11',30),('2026-11-12',40)]]
        history=dict(status='ok',date='2026-11-12',history=rows)
        merged=merge_history(snapshot,history)
        self.assertEqual(merged['three_day_net'],120);self.assertEqual(merged['five_day_net'],110)
        self.assertEqual(merged['positive_days'],3)
        self.assertEqual(rows[-1]['date'],'2026-11-12');self.assertEqual(len(rows),4)
        self.assertIsNone(merge_history(dict(snapshot,date='2026-11-16'),merged)['positive_days'])
        snapshot['date']='2026-11-16'  # Friday history has not arrived: do not bridge the gap.
        self.assertIsNone(merge_history(snapshot,history)['positive_days'])

    def test_incomplete_members_are_rejected_not_counted_as_full_sector(self):
        service=MainlineService(FundFlowService())
        with patch.object(service.funds,'_json',return_value={'data':{'total':2,'diff':[{'f12':'600001'}]}}):
            with self.assertRaises(ValueError):service.members('BK0001')

    def test_wrong_day_pool_is_unknown(self):
        service=MainlineService(FundFlowService())
        with patch.object(service.funds,'_json',return_value={'data':{'qdate':20261109,'pool':[]}}):
            self.assertEqual(service.limit_pool(DATE)['status'],'unavailable')

    def test_partial_pool_is_unknown_instead_of_reporting_zero(self):
        service=MainlineService(FundFlowService())
        with patch.object(service.funds,'_json',return_value={'data':{'qdate':20261110,'tc':5,'pool':[]}}):
            self.assertIsNone(service.limit_pool(DATE).get('net'))
            self.assertEqual(service.limit_pool(DATE)['status'],'unavailable')

    def test_shortlist_has_no_two_stock_sectors_and_deduplicates_overlap(self):
        funds=FundFlowService(); service=MainlineService(funds)
        sectors={}
        for i,n in enumerate([2,20,25,30]):
            sectors[str(i)] = dict(status='ok',name='行业'+str(i),sector_code='BK000'+str(i),date=DATE,
                net=(i+1)*1e8,ratio=9-i,pct=3, rise_count=n,fall_count=0,flat_count=0)
        def members(code):
            # First two eligible sectors overlap; only one should survive.
            base=0 if code in ('BK0001','BK0002') else 100
            return [dict(stock(f'{600000+base+j}'),quote_date=DATE) for j in range(20)]
        with patch.object(funds,'sectors',return_value={'status':'ok','items':sectors}), \
             patch.object(funds,'sector_history',return_value={'status':'unavailable'}), \
             patch.object(funds,'stock',return_value={'status':'unavailable'}), \
             patch.object(service,'members',side_effect=members) as fetch, \
             patch.object(service,'limit_pool',return_value={'status':'ok','items':{}}):
            result=service.build()
        self.assertNotIn('行业0',[g['name'] for g in result['groups']])
        self.assertEqual(len(result['groups']),2)
        self.assertEqual(fetch.call_count,3)


if __name__=='__main__':unittest.main()
