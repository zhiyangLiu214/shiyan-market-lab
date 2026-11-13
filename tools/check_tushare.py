"""Read-only permission probe. Must be explicitly run; never creates an account."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from terminal_tushare import TushareData


def main():
    p=argparse.ArgumentParser(description='逐项核验Tushare数据权限，不执行任何交易')
    p.add_argument('--code',default='000001');args=p.parse_args()
    client=TushareData(Path(__file__).resolve().parents[1]/'.runtime'/'tushare-token')
    if not client.configured():raise SystemExit('未配置Token；没有发送任何付费接口请求。')
    failures=0
    for label,read in [('日线及盘中日线',lambda:client.prices('daily',args.code)),
                       ('当日完整分钟',lambda:client.prices('minute',args.code)),
                       ('个股盘后资金历史',lambda:client.money_history(args.code)),
                       ('板块盘后资金历史',lambda:client.money_history('BK0475',sector=True))]:
        try:
            data=read();count=len(data) if hasattr(data,'columns') else len(data['history'])
            print(label+'：通过；'+str(count)+'条已校验记录')
        except Exception:
            failures+=1
            print(label+'：未通过；请核验对应权限、数据日期与接口状态。')
    print('检查完成，不代表所有权限均已通过；历史资金不作为今日资金。')
    return 2 if failures else 0


if __name__=='__main__':raise SystemExit(main())
