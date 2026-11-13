#!/bin/zsh
set -eu
PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
RUNTIME_DIR="$PROJECT_DIR/.runtime"
URL='http://127.0.0.1:8766'
mkdir -p "$RUNTIME_DIR"
if /usr/bin/curl --noproxy '*' -fsS --max-time 2 "$URL/" 2>/dev/null | /usr/bin/grep -q '势研'; then
  print "势研已在运行，请在这台 Mac 的浏览器打开：$URL"
  exit 0
fi
if [[ ! -x "$PROJECT_DIR/.venv/bin/gunicorn" ]]; then
  print '缺少运行环境，请按 README.md 安装依赖。'
  exit 1
fi
export PYTHONPYCACHEPREFIX="$RUNTIME_DIR/pycache"
"$PROJECT_DIR/.venv/bin/gunicorn" --chdir "$PROJECT_DIR" \
  --workers 1 --threads 8 --timeout 60 --bind 127.0.0.1:8766 \
  --pid "$RUNTIME_DIR/server.pid" --error-logfile "$RUNTIME_DIR/server.log" \
  --access-logfile "$RUNTIME_DIR/access.log" --daemon app:app
for attempt in {1..20}; do
  if /usr/bin/curl --noproxy '*' -fsS --max-time 2 "$URL/" >/dev/null 2>&1; then
    print "势研已启动。请在这台 Mac 的浏览器打开：$URL"
    print '关闭本窗口不会停止服务。电脑重启后请重新双击本文件。'
    exit 0
  fi
  sleep 0.5
done
print "启动未完成，请查看日志：$RUNTIME_DIR/server.log"
exit 1
