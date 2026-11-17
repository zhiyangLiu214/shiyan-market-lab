# 共享回测研究库

云端 Docker MySQL 映射宿主机 TCP `4235` 到容器 `3306`，数据目录映射到 `/app/.runtime/shared-mysql/data`，容器重启保留原有数据。研究库与网站的 SQLite 策略、模拟账户及运行目录分离。服务器仍保留专用 SSH 隧道账号；需要直连的协作者也可使用管理员私下提供的 MySQL 账号。远程 `root@'%'` 拥有全部数据库权限，密码只在服务器和授权协作者的私有文件中保存，不写入仓库。服务器本机和容器端口已验证；公网直连还需云安全组放行入站 TCP `4235`，在此之前请使用 SSH 隧道。

容器定义在 [Compose 配置](../deploy/shared-mysql.compose.yaml)。`/app/.runtime/shared-mysql/secrets/root-password` 和 `remote-root-password` 是权限为 `0600` 的服务器私有文件，分别供容器本机 root 和远程 root 使用，不随代码部署。更新镜像前需备份映射目录并核验恢复流程；代码部署不删除或覆盖数据目录。

只有已封存且在 `list` 中可见的数据集才能用于双方回测。开始共享回测前，管理员需核验文件数、清单哈希和覆盖日期。

当前冻结数据集 `replay-2026-11-17-v1` 的编号为 `97ab939d-81fd-42a8-b221-c27b30143fcf`，包含 38,926 份 JSON，清单 SHA-256 为 `c401b1dff5746cd9d6ee8c828f5e49c5a458f7c0caa70f9e98f8dd9dea6c9fa5`。上传归档的 SHA-256 为 `04ba8eedb5c6aad83beed1b15e36b7d02c8ded949a0d04c1c724eed36c16eec9`；归档保存在服务器私有暂存目录。封存后已逐份核对数据库内容哈希。它是制作归档时的快照，不随本地缓存后续新增文件自动变化。

## 数据边界

- `datasets` 和 `files` 保存冻结的回测输入：`daily/`、`bars/`、`ticks/`、`actions/`、`dividends/` 下的 JSON，以及 `roster.json`、`strategies.json`。每个文件保留来源、源时间、获取时间和 SHA-256；缺失字段保持缺失。
- `runs` 保存完成或部分完成的回测报告。协作者通过 `own_runs` 视图读取自己的结果，通过存储过程写入；不能直接访问他人的结果或修改共享输入。
- 私有策略配置经 `tools/strategy_transfer.py` 导出，再发布至 `strategy_bundles`；拉取后由同一工具导入本地。共享研究库不读取生产账户、持仓、成交、凭证或笔记数据库。
- 冻结数据集是一个明确版本；后续更新需发布新数据集。不能把新旧行情静默混用。

## 协作者本地使用

先安装当前仓库依赖以及 `python -m pip install PyMySQL`。从管理员私下取得数据库账号、密码和数据集编号，不要加入 Git。公网直连时设置 `TDX_RESEARCH_HOST=111.91.25.20`、`TDX_RESEARCH_PORT=4235`；原有本机 `3306` 映射与 SSH 隧道继续可用：

```bash
ssh -N -L 13306:127.0.0.1:3306 research-share@111.91.25.20
```

另一个终端设置 `TDX_RESEARCH_USER` 和 `TDX_RESEARCH_PASSWORD_FILE`（只含密码，权限 `0600`）；隧道模式默认连接 `127.0.0.1:13306`。随后运行：

```bash
python tools/shared_research.py list
python tools/shared_research.py checkout --dataset 数据集UUID --destination .runtime/replay
python tools/shared_research.py upload-run --dataset 数据集UUID --report .runtime/backtests/运行编号/report.json
python tools/shared_research.py list-runs
python tools/shared_research.py download-run --run-id 结果UUID --output .runtime/shared-run.json
python tools/shared_research.py list-bundles
python tools/shared_research.py download-strategies --bundle-id 策略包UUID --output .runtime/private.strategy-bundle.json
python tools/strategy_transfer.py import --runtime .runtime --bundle .runtime/private.strategy-bundle.json --apply
```

拉取不会覆盖内容不同的本地文件。回测仍在本地运行，输入目录应明确指向拉取的数据集；报告须保留数据集编号和代码版本，便于重现。上传 `partial` 报告不代表完整收益结论。

## 管理员发布

管理员账号通过同样的本地隧道连接，并发布经过核验的数据目录：

```bash
python tools/shared_research.py publish --root .runtime/replay --name replay-2026-11-16-v1
python tools/strategy_transfer.py export --runtime .runtime --output .runtime/private.strategy-bundle.json
python tools/shared_research.py publish-strategies --bundle .runtime/private.strategy-bundle.json --name strategies-2026-11-16-v1
```

发布命令逐个上传允许目录中的 JSON，完成后才标记为已封存。若中途失败，清理未封存数据集。大规模分笔数据上传可能持续较久；期间保持隧道连接。数据库和文件备份由云端运维单独管理，不能用代码更新覆盖。
