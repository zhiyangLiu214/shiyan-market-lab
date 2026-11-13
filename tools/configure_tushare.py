"""Run on the website host; hidden prompt, private file, no token in command history."""
import argparse
import getpass
import os
from pathlib import Path
import tempfile


def save_token(path, token):
    token=token.strip()
    if not 16<=len(token)<=256 or any(c.isspace() for c in token):
        raise ValueError('Token格式无效；请从官方账号复制完整Token')
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.tushare-',dir=str(path.parent))
    try:
        os.fchmod(fd,0o600)
        if hasattr(os,'geteuid') and os.geteuid()==0:
            owner=path.parent.stat();os.fchown(fd,owner.st_uid,owner.st_gid)
        with os.fdopen(fd,'w') as file:file.write(token+'\n')
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def main():
    parser=argparse.ArgumentParser(description='在网站服务器保存Tushare凭证，不购买或开通权限')
    parser.add_argument('--path',type=Path,default=Path(__file__).resolve().parents[1]/'.runtime'/'tushare-token')
    args=parser.parse_args()
    if not os.isatty(0):raise SystemExit('请在交互终端执行，避免把Token写入命令行或脚本')
    token=getpass.getpass('Tushare Token（输入不显示）：')
    save_token(args.path,token)
    print('凭证已保存为600权限文件。未购买或开通权限；请运行检查工具验证。')


if __name__=='__main__':main()
