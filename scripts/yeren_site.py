"""Private publishing configuration stays in ignored runtime files or environment."""
import base64
import json
import netrc
import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]


def site_access():
    path=ROOT/'.runtime'/'notes-publish.json'
    config=json.loads(path.read_text()) if path.exists() else {}
    base=(os.environ.get('SHIYAN_BASE_URL') or config.get('base_url') or 'http://127.0.0.1:8766').rstrip('/')
    parsed=urlsplit(base)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('请配置不含凭证或查询参数的笔记站点地址')
    credentials=Path(os.environ.get('SHIYAN_NETRC') or config.get('netrc_path') or Path.home()/'.netrc').expanduser()
    auth=netrc.netrc(str(credentials)).authenticators(parsed.hostname) if credentials.exists() else None
    headers={'Content-Type':'application/json'}
    if auth:
        user,_,secret=auth
        headers['Authorization']='Basic '+base64.b64encode((user+':'+secret).encode()).decode()
    elif parsed.hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('远程站点尚未配置匹配主机的netrc认证')
    return base+'/api/terminal/notes',headers
