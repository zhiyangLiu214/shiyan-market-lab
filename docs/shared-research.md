# 共享回测研究库

云端 Docker MySQL 仅绑定 `127.0.0.1:3306`，数据目录映射到 `/app/.runtime/shared-mysql/data`。协作者使用专用 SSH 公钥隧道访问，不开放数据库公网端口。研究库与网站的 SQLite 策略、模拟账户及运行目录分离。

容器定义在 [Compose 配置](../deploy/shared-mysql.compose.yaml)。`/app/.runtime/shared-mysql/secrets/root-password` 是权限为 `0600` 的服务器私有文件，不随代码部署。更新镜像前需备份映射目录并核验恢复流程；代码部署不删除或覆盖数据目录。

只有已封存且在 `list` 中可见的数据集才能用于双方回测。开始共享回测前，管理员需核验文件数、清单哈希和覆盖日期。

## 数据边界

- `datasets` 和 `files` 保存冻结的回测输入：`daily/`、`bars/`、`ticks/`、`actions/`、`dividends/` 下的 JSON，以及 `roster.json`、`strategies.json`。每个文件保留来源、源时间、获取时间和 SHA-256；缺失字段保持缺失。
- `runs` 保存完成或部分完成的回测报告。协作者通过 `own_runs` 视图读取自己的结果，通过存储过程写入；不能直接访问他人的结果或修改共享输入。
- 私有策略配置经 `tools/strategy_transfer.py` 导出，再发布至 `strategy_bundles`；拉取后由同一工具导入本地。共享研究库不读取生产账户、持仓、成交、凭证或笔记数据库。
- 冻结数据集是一个明确版本；后续更新需发布新数据集。不能把新旧行情静默混用。

## 协作者本地使用

先安装当前仓库依赖以及 `python -m pip install PyMySQL`。从管理员私下取得数据库账号、密码和数据集编号，不要加入 Git。使用提供的 SSH 公钥对应的私钥建立隧道：

```bash
ssh -N -L 13306:127.0.0.1:3306 research-share@111.91.25.20
```

另一个终端设置 `TDX_RESEARCH_USER` 和 `TDX_RESEARCH_PASSWORD_FILE`（只含密码，权限 `0600`）；默认连接 `127.0.0.1:13306`。随后运行：

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
