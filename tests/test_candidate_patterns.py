import copy
import unittest
from tools.research_candidate_patterns import entry_features,forward_labels


class CandidateFeatureTests(unittest.TestCase):
    def test_entry_features_use_partial_volume_and_only_given_past(self):
        rows=[dict(open=10,close=10,high=11,low=9,volume=100) for _ in range(70)]
        rows[-1]=dict(open=10,close=10.5,high=11,low=9.5,volume=200)
        result=entry_features(rows,dict(change_today=1,change_5=2))
        self.assertEqual(result['volume_ratio'],2)
        self.assertAlmostEqual(result['change_today'],5)
        self.assertAlmostEqual(result['relative_change_5'],3)
        self.assertAlmostEqual(result['close_location'],2/3)
        self.assertNotIn('future_return',result)
        self.assertEqual(rows[-1]['volume'],200)

    def test_forward_rise_and_stop_zone_can_overlap(self):
        days=['2026-11-02','2026-11-03','2026-11-04','2026-11-05','2026-11-06','2026-11-09']
        raw=[[d,10,10,10.6,9.4,100] for d in days]
        data=dict(raw=raw,qfq=copy.deepcopy(raw))
        labels=forward_labels(data,[],days[0],days[1:])
        self.assertTrue(labels['rise_5']);self.assertTrue(labels['touch_minus5'])
        self.assertFalse(labels['rise_10'])
        self.assertFalse(forward_labels(data,[],days[0],days[1:4])['complete'])
        self.assertAlmostEqual(labels['close_return_pct'],0)

    def test_cash_dividend_does_not_create_false_negative_forward_price_label(self):
        days=['2026-11-02','2026-11-03','2026-11-04','2026-11-05','2026-11-06','2026-11-09']
        raw=[[days[0],10,10,10,10,100]]+[[d,9,9,9,9,100] for d in days[1:]]
        qfq=[[d,9,9,9,9,100] for d in days]
        labels=forward_labels(dict(raw=raw,qfq=qfq),[dict(date=days[1],category=1,fenhong=10)],days[0],days[1:])
        self.assertAlmostEqual(labels['close_return_pct'],0)
        self.assertFalse(labels['touch_minus5'])


class EntryLearningTests(unittest.TestCase):
    def test_future_and_unresolved_labels_cannot_change_training_model(self):
        import math
        from tools.learn_entry_rules import fit_entry_model,predict_entry,policy_accept,POLICIES
        rows=[dict(status='complete',date='2026-01-02',observed_end='2026-01-09',
                   net_return_pct=i*.1-2,loss_stop_exit=i<20,
                   forward=dict(rise_5=i>=30),
                   features=dict(change_today=float(i),gap_open=math.sin(i),volume_ratio=float(i%7+1))) for i in range(60)]
        original=fit_entry_model(rows,'2026-01-31')
        future=copy.deepcopy(rows[0]);future.update(date='2026-02-02',observed_end='2026-02-09',net_return_pct=100000.)
        unresolved=copy.deepcopy(future);unresolved.update(date='2026-01-30')
        self.assertEqual(original,fit_entry_model(rows+[future,unresolved],'2026-01-31'))
        expected=predict_entry(original,rows[10]['features'])
        changed=dict(rows[10]['features'],future_return=100000.)
        self.assertEqual(expected,predict_entry(original,changed))
        self.assertGreaterEqual(expected['stop_probability'],0);self.assertLessEqual(expected['stop_probability'],1)
        self.assertFalse(policy_accept(None,POLICIES[0]))
        self.assertFalse(policy_accept(dict(expected_net_pct=-1,stop_probability=.2),POLICIES[1]))
class VolumeFeatureTests(unittest.TestCase):
    def test_same_clock_volume_ignores_closing_half_hour_and_future(self):
        from datetime import datetime,timedelta
        from tools.augment_volume_features import volume_price_features
        days=[(datetime(2026,1,1)+timedelta(days=i)).strftime('%Y-%m-%d') for i in range(22)]
        raw=[[d,10,10,11,9,1000] for d in days];daily=dict(raw=raw,qfq=copy.deepcopy(raw));bars=[]
        for i,d in enumerate(days):
            for time in ['1000','1030','1100','1130','1330','1400','1430','1500']:
                bars.append([d.replace('-','')+time,10,10,11,9,20 if i==20 else 10])
        result=volume_price_features(daily,bars,days[20])
        self.assertEqual(result['same_time_volume_ratio_5'],2)
        self.assertEqual(result['same_time_volume_ratio_20'],2)
        self.assertEqual(result['last30_volume_acceleration'],1)
        for row in bars:
            if row[0][:8]>days[20].replace('-','') or row[0]==days[20].replace('-','')+'1500':row[2]=1000;row[5]=1000000
        daily['raw'][20][5]=9999999
        self.assertEqual(result,volume_price_features(daily,bars,days[20]))

    def test_volume_profile_uses_two_volume_features_and_one_price_feature(self):
        import math
        from tools.learn_entry_rules import fit_entry_model,VOLUME_FEATURES
        rows=[dict(status='complete',date='2026-01-02',observed_end='2026-01-09',net_return_pct=i*.1-2,
                   loss_stop_exit=i<20,forward=dict(rise_5=i>=30),features=dict(change_today=float(i),
                   gap_open=math.sin(i),volume_ratio=float(i%7+1),volume_5_over_20=math.cos(i)*.5+1)) for i in range(60)]
        rows[0]['features']['volume_ratio']=None
        model=fit_entry_model(rows,'2026-01-31',profile='volume_price')
        self.assertEqual(sum(f['feature'] in VOLUME_FEATURES for f in model['features']),2)
        self.assertEqual(len(model['features']),3)
        self.assertEqual(model['training_count'],59)
        self.assertEqual(model['dropped_missing_count'],1)


class VolumeCellTests(unittest.TestCase):
    def test_interaction_rules_use_only_resolved_training_labels(self):
        from tools.test_volume_cells import fit_cells,cell_score
        rows=[dict(status='complete',date=f'2026-01-{(i//9)%20+1:02}',observed_end='2026-01-25',
                   net_return_pct=3 if i%3==2 else -1,actual_profit=i%3==2,loss_stop_exit=i%3!=2,
                   forward=dict(rise_5=i%3==2),features=dict(a=float(i%3),b=float((i//3)%3))) for i in range(360)]
        model=fit_cells(rows,'2026-01-31','a','b')
        future=copy.deepcopy(rows[0]);future.update(date='2026-02-01',observed_end='2026-02-05',net_return_pct=100000.)
        self.assertEqual(model,fit_cells(rows+[future],'2026-01-31','a','b'))
        self.assertGreater(cell_score(model,dict(a=2,b=2)),0)
        self.assertLess(cell_score(model,dict(a=0,b=0)),0)
        self.assertIsNone(cell_score(model,dict(a=None,b=0)))


if __name__=='__main__':unittest.main()
