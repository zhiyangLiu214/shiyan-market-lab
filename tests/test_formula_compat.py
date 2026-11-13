import unittest
from terminal_formula import compile_formula,evaluate_formula,FormulaError

class CompatibilityTests(unittest.TestCase):
    def rows(self,n=80):return [dict(open=10,high=11,low=9,close=10,volume=100) for _ in range(n)]
    def test_multiline_semicolon_assignments_and_64_statement_bound(self):
        text='A:=IF(C>0,\n 1,\n 0);\n选股:A=1\n AND C>O-1;'
        self.assertEqual(evaluate_formula(text,self.rows()),1)
        self.assertEqual(len(compile_formula(';'.join('A'+str(i)+':=C' for i in range(64)))),64)
        with self.assertRaises(FormulaError):compile_formula(';'.join('A'+str(i)+':=C' for i in range(65)))
    def test_conditional_division_and_false_guard_are_not_failures(self):
        rows=self.rows();rows[-1].update(high=10,low=10)
        self.assertEqual(evaluate_formula('R:=H-L;IF(R>0,(H-C)/R,0)',rows),0)
        self.assertEqual(evaluate_formula('R:=H-L;R>0 AND (H-C)/R<0.3',rows),0)
        with self.assertRaises(FormulaError):evaluate_formula('C/0>0',rows)
    def test_barslast_and_barscount(self):
        rows=self.rows();rows[-4]['volume']=50
        self.assertEqual(evaluate_formula('BARSLAST(V=LLV(V,20))',rows),3)
        self.assertEqual(evaluate_formula('BARSCOUNT(C)',rows),80)
        self.assertEqual(evaluate_formula('BARSLAST(C>100)',rows),80)
        self.assertEqual(evaluate_formula('BARSCOUNT(MA(C,20))',rows),61)
    def test_code_prefix_and_half_up_limit_price(self):
        formula="R:=IF(CODELIKE('300') OR CODELIKE('688'),0.2,0.1);ZTPRICE(10.05,R)"
        self.assertEqual(evaluate_formula(formula,self.rows(),code='600001'),11.06)
        self.assertEqual(evaluate_formula(formula,self.rows(),code='300001'),12.06)
        with self.assertRaises(FormulaError):evaluate_formula("CODELIKE('6')",self.rows())
        for text in ["CODELIKE('__import__')","IF(1,'text',0)","CODELIKE(C)","CODELIKE('6',1)"]:
            with self.subTest(text=text),self.assertRaises(FormulaError):compile_formula(text)
    def test_doji_lookback_and_breakout_on_synthetic_rows(self):
        rows=self.rows();rows[-4].update(volume=10,open=10,close=10.01,high=10.5,low=9.5)
        rows[-1].update(open=10.5,close=11.2,high=11.3,low=10.4,volume=200)
        formula="D:=H>L AND ABS(C-O)<=(H-L)*0.1 AND V=LLV(V,20);T:=BARSLAST(D);B:=REF(HHV(H,20),1);XG:BARSCOUNT(C)>60 AND T>=1 AND T<=10 AND C>B AND C>O*1.05 AND V>REF(MA(V,5),1)*1.8 AND C<ZTPRICE(REF(C,1),0.2);"
        self.assertEqual(evaluate_formula(formula,rows),1)
        rows[-1]['volume']=100
        self.assertEqual(evaluate_formula(formula,rows),0)

if __name__=='__main__':unittest.main()
