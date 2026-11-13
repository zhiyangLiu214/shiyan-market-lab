"""Use on an authorized QMT client machine; only query public market snapshots."""
import importlib.util
import json


def main():
    if importlib.util.find_spec('xtquant') is None:
        print('未检测到xtquant；请先确认券商QMT／迅投客户端和数据权限。')
        return 2
    from xtquant import xtdata
    try:
        data=xtdata.get_full_tick(['000001.SZ'])
        row=(data or {}).get('000001.SZ') or {}
        print(json.dumps(dict(snapshot_received=bool(row),source_time=row.get('time'),
                             price=row.get('lastPrice'),
                             note='仅核验行情连通性；未验证资金流权限，未接入网站，未执行交易'),ensure_ascii=False))
        return 0 if row else 2
    except Exception:
        print('未取得行情快照；请确认客户端运行状态和数据权限。')
        return 2


if __name__=='__main__':raise SystemExit(main())
