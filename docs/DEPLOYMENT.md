# 部署与更新

建议使用 Python 3.11、单进程 Gunicorn，反向代理负责访问控制与 HTTPS。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/gunicorn --workers 1 --threads 8 --timeout 120 --bind 127.0.0.1:8766 app:app
```

仓库内 `tdx-dashboard.service` 是 `/app` 路径的示例，服务账号为 `shiyan`；安装前应按实际环境准备账号、目录权限与反向代理。不会通过克隆仓库自动修改系统服务。

更新流程：

1. 拉取并检查指定分支/提交，安装确有变化的依赖。
2. 备份现有代码及部署版本，保持 `.runtime/` 的归属和内容。
3. 更新代码，重启应用，检查首页、`/api/terminal/sources` 与 `/api/terminal/paper`。
4. 核对模拟账户启动时间、资金、持仓与成交；失败时还原上一个代码版本。

禁止通过整目录覆盖或同步删除 `.runtime/`。公开仓库不包含生产环境地址、访问密码或已有账本。

Tushare 可选配置参见 [数据源配置](../research/data-source-setup.md)。未配置或未通过权限验证的源不能当作已接通数据。

系统桌面通知需要受信任的HTTPS、Python3.10及以上的Web Push发送组件，并配置实例的 `SHIYAN_PUBLIC_ORIGIN` 或 `.runtime/web-push/config.json` 中的 `public_origin`。Nginx仍应保护首页、订阅API和Service Worker，只有证书校验路径可按签发方要求公开。不要把VAPID私钥、浏览器订阅或发送队列放入Git。备份、更新时保留整个 `.runtime/web-push/`，丢失签名私钥会使已有浏览器订阅不可用。

IP证书有效期较短，应设置定期续期及成功后的Nginx重载；上线前同时验证公网证书、原访问认证、订阅端点和后台通知测试。浏览器订阅需要用户点“允许”，推送通道接受只表示请求成功，最终横幅仍受设备网络、浏览器及系统通知设置影响。

笔记发布脚本默认指向本机服务。发布到自己的站点时，设置 `SHIYAN_BASE_URL` 和 `SHIYAN_NETRC`，或在本机 `.runtime/notes-publish.json` 中配置 `base_url`、`netrc_path`。凭证放在权限为600的 netrc 文件中，不能放进 URL 或源码；脚本默认只预览，需要显式 `--publish` 才会写入。
