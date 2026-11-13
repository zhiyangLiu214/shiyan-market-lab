"""Small, explicit Tongdaxin-style formula interpreter; never executes Python code."""
import ast
import re
import math
import operator
from decimal import Decimal,ROUND_HALF_UP

import pandas as pd

ALIASES={'C':'close','CLOSE':'close','O':'open','OPEN':'open','H':'high','HIGH':'high',
         'L':'low','LOW':'low','V':'volume','VOL':'volume','VOLUME':'volume','AMOUNT':'amount'}
FUNCTIONS={'MA','EMA','SMA','REF','HHV','LLV','SUM','COUNT','EVERY','EXIST','CROSS','ABS','MAX','MIN','IF','BARSLAST','BARSCOUNT','CODELIKE','NAMELIKE','ZTPRICE'}
BINARY={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv}
COMPARE={ast.Gt:operator.gt,ast.GtE:operator.ge,ast.Lt:operator.lt,ast.LtE:operator.le,ast.Eq:operator.eq,ast.NotEq:operator.ne}


class FormulaError(ValueError):pass


def compile_formula(text):
    if not isinstance(text,str) or not text.strip() or len(text)>4000:raise FormulaError('公式须为1至4000个字符')
    source=re.sub(r'\{[^{}]*\}','',text)
    parts=re.split('[;；]+',source) if re.search('[;；]',source) else source.splitlines()
    statements=[' '.join(s.split()) for s in parts if s.strip()]
    if not 1<=len(statements)<=64:raise FormulaError('公式最多64条语句，使用分号分隔')
    known=set(ALIASES);result=[]
    for i,statement in enumerate(statements):
        match=re.fullmatch(r'([^:：]+)(:=|:|：)(.+)',statement)
        name=None;expression=statement
        if match:
            name=match[1].strip().upper();expression=match[3].strip()
            if not name.isidentifier() or name.startswith('_') or name in known or name in FUNCTIONS or len(name)>40:
                raise FormulaError('变量名无效、重复或占用了内置字段')
        expression=re.sub(r'\bAND\b','and',expression,flags=re.I)
        expression=re.sub(r'\bOR\b','or',expression,flags=re.I)
        expression=re.sub(r'\bNOT\b','not',expression,flags=re.I)
        expression=expression.replace('&&',' and ').replace('||',' or ').replace('<>','!=')
        expression=re.sub(r'(?<![<>=!:])=(?!=)','==',expression)
        try:tree=ast.parse(expression.strip(),mode='eval')
        except SyntaxError:raise FormulaError('公式语法无效；请使用支持的通达信选股表达式') from None
        nodes=list(ast.walk(tree))
        if len(nodes)>300:raise FormulaError('公式过于复杂')
        allowed=(ast.Expression,ast.BinOp,ast.BoolOp,ast.UnaryOp,ast.Compare,ast.Call,ast.Name,ast.Constant,ast.Load,
                 ast.Add,ast.Sub,ast.Mult,ast.Div,ast.And,ast.Or,ast.Not,ast.UAdd,ast.USub,*COMPARE)
        strings={id(call.args[0]):call.func.id.upper() for call in nodes if isinstance(call,ast.Call) and isinstance(call.func,ast.Name) and call.func.id.upper() in ('CODELIKE','NAMELIKE') and len(call.args)==1}
        for node in nodes:
            if not isinstance(node,allowed):raise FormulaError('不支持属性、下标、代码调用或该公式运算')
            if isinstance(node,ast.Name) and node.id.upper() not in known|FUNCTIONS:raise FormulaError('未定义或不支持的字段：'+node.id)
            if isinstance(node,ast.Constant) and id(node) in strings:
                pattern=r'\d{1,6}' if strings[id(node)]=='CODELIKE' else r'[\w*·.\-]{1,30}'
                if not isinstance(node.value,str) or not re.fullmatch(pattern,node.value):raise FormulaError('CODELIKE需要数字前缀；NAMELIKE需要1至30字名称前缀')
                continue
            if isinstance(node,ast.Constant) and (type(node.value) not in (int,float,bool) or not math.isfinite(node.value) or abs(node.value)>1e12):
                raise FormulaError('仅支持有限数值常量')
            if isinstance(node,ast.Call):
                if not isinstance(node.func,ast.Name) or node.func.id.upper() not in FUNCTIONS or node.keywords:raise FormulaError('仅支持列出的公式函数')
                expected=3 if node.func.id.upper() in ('SMA','IF') else 1 if node.func.id.upper() in ('ABS','BARSLAST','BARSCOUNT','CODELIKE','NAMELIKE') else 2
                if len(node.args)!=expected:raise FormulaError(node.func.id+'参数数量无效')
                if node.func.id.upper() in ('CODELIKE','NAMELIKE') and (not isinstance(node.args[0],ast.Constant) or not isinstance(node.args[0].value,str)):raise FormulaError('CODELIKE/NAMELIKE需要前缀字符串')
        result.append((name,tree.body))
        if name:known.add(name)
    return result


def evaluate_formula(text,bars,code=None,name=None):
    stock_name=name
    statements=compile_formula(text);frame=pd.DataFrame(bars)
    if frame.empty:raise FormulaError('缺少日线数据')
    env={name:pd.to_numeric(frame[column],errors='coerce') for name,column in ALIASES.items() if column in frame}
    def series(value):return value if isinstance(value,pd.Series) else pd.Series(value,index=frame.index)
    def truth(value):
        x=series(value);return x.ne(0).astype('boolean').mask(x.isna()|x.map(lambda v:False if pd.isna(v) else not math.isfinite(float(v))))
    def checked(value):
        last=value.iloc[-1] if isinstance(value,pd.Series) else value
        if pd.isna(last) or not math.isfinite(float(last)):raise FormulaError('公式所需历史不足或计算结果无效')
        return value
    def window(value):
        if not isinstance(value,(int,float)) or isinstance(value,bool) or int(value)!=value or not 1<=value<=250:raise FormulaError('周期必须为1至250的整数')
        if len(frame)<value:raise FormulaError('日线不足'+str(int(value))+'根')
        return int(value)
    def run(node,allow_invalid=False):
        check=(lambda value:value) if allow_invalid else checked
        child=lambda n:run(n,allow_invalid)
        if isinstance(node,ast.Constant):return node.value
        if isinstance(node,ast.Name):
            if node.id.upper() not in env:raise FormulaError('源未提供字段：'+node.id)
            return check(env[node.id.upper()])
        if isinstance(node,ast.BinOp):return check(BINARY[type(node.op)](child(node.left),child(node.right)))
        if isinstance(node,ast.UnaryOp):
            value=child(node.operand)
            if isinstance(node.op,ast.Not):return ~truth(value)
            return check(-value if isinstance(node.op,ast.USub) else value)
        if isinstance(node,ast.BoolOp):
            values=[truth(run(n,allow_invalid=True)) for n in node.values];value=values[0]
            for other in values[1:]:value=(value&other) if isinstance(node.op,ast.And) else (value|other)
            return value
        if isinstance(node,ast.Compare):
            left=child(node.left);result=pd.Series(True,index=frame.index,dtype='boolean')
            for op,right_node in zip(node.ops,node.comparators):
                right=child(right_node);part=series(COMPARE[type(op)](left,right)).astype('boolean')
                invalid=lambda v:series(v).map(lambda x:pd.isna(x) or not math.isfinite(float(x)))
                part=part.mask(invalid(left)|invalid(right));result &= part;left=right
            return result
        if isinstance(node,ast.Call):
            name=node.func.id.upper()
            if name=='CODELIKE':
                if not isinstance(code,str) or not re.fullmatch(r'\d{6}',code):raise FormulaError('CODELIKE缺少有效股票代码上下文')
                return code.startswith(node.args[0].value)
            if name=='NAMELIKE':
                if not isinstance(stock_name,str) or not stock_name.strip() or stock_name.strip()==code:raise FormulaError('NAMELIKE缺少已核验的股票名称上下文')
                return stock_name.strip().casefold().startswith(node.args[0].value.casefold())
            if name=='IF':
                condition=truth(run(node.args[0]));leftmask=condition.fillna(False);rightmask=(~condition).fillna(False)
                # Evaluate only branches actually used, allowing guarded division.
                def branch(expr,mask):
                    if not mask.any():return series(float('nan'))
                    return series(run(expr,allow_invalid=True))
                left=branch(node.args[1],leftmask);right=branch(node.args[2],rightmask)
                return check(left.where(leftmask,right).mask(condition.isna()))
            args=[child(a) for a in node.args]
            expected=3 if name in ('SMA','IF') else 1 if name in ('ABS','BARSLAST','BARSCOUNT','CODELIKE','NAMELIKE') else 2
            if len(args)!=expected:raise FormulaError(name+'参数数量无效')
            x=series(args[0])
            if name=='BARSLAST':
                valid=x.notna();last=None;values=[]
                for index,flag in enumerate(truth(x)):
                    if pd.notna(flag) and bool(flag):last=index
                    values.append(float('nan') if not valid.iloc[index] else index-last if last is not None else index+1)
                value=pd.Series(values,index=frame.index)
            elif name=='BARSCOUNT':
                indices=[i for i,v in enumerate(x.notna()) if v]
                first=indices[0] if indices else len(x)
                value=pd.Series([max(0,i-first+1) for i in range(len(x))],index=frame.index)
            elif name=='ZTPRICE':
                y=series(args[1]);value=pd.Series(float('nan'),index=frame.index)
                for idx in frame.index:
                    if pd.notna(x.loc[idx]) and pd.notna(y.loc[idx]):
                        value.loc[idx]=float((Decimal(str(x.loc[idx]))*(1+Decimal(str(y.loc[idx])))).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
            elif name=='ABS':value=x.abs()
            elif name in ('MAX','MIN'):
                pair=pd.concat([x,series(args[1])],axis=1);value=pair.max(axis=1,skipna=False) if name=='MAX' else pair.min(axis=1,skipna=False)
            elif name=='CROSS':
                if len(frame)<2:raise FormulaError('CROSS至少需要2根日线')
                y=series(args[1])
                if pd.isna(x.iloc[-2]) or pd.isna(y.iloc[-2]):raise FormulaError('CROSS历史不足')
                value=(x>y)&(x.shift(1)<=y.shift(1))
            else:
                n=window(args[1])
                if name=='REF':value=x.shift(n)
                elif name=='MA':value=x.rolling(n).mean()
                elif name=='EMA':value=x.ewm(span=n,adjust=False).mean()
                elif name=='SMA':
                    m=args[2]
                    if not isinstance(m,(int,float)) or not 0<m<=n:raise FormulaError('SMA第三参数须大于0且不超过周期')
                    value=x.ewm(alpha=m/n,adjust=False).mean()
                elif name=='HHV':value=x.rolling(n).max()
                elif name=='LLV':value=x.rolling(n).min()
                elif name=='SUM':value=x.rolling(n).sum()
                else:
                    count=truth(x).astype('Float64').rolling(n).sum()
                    value=count if name=='COUNT' else (count==n).where(count.notna()) if name=='EVERY' else (count>0).where(count.notna())
            return check(value)
        raise FormulaError('不支持的公式节点')
    value=None
    for name,tree in statements:
        value=checked(run(tree))
        if name:env[name]=value
    return float(value.iloc[-1] if isinstance(value,pd.Series) else value)
