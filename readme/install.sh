#!/usr/bin/env bash
# 云影图鉴 · 一键安装（最小可运行路径）
# 适用：Debian 12 / Ubuntu 22.04，root 或 sudo 执行
# 特性：每步幂等（重复执行无副作用）；任一步失败自动回滚 systemd/nginx 配置
# 用法：在仓库检出目录内执行  bash readme/install.sh
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/yunying}
SVC=${SVC:-yunying-demo}
TS=$(date +%Y%m%d%H%M%S)
STEP="init"

rollback() {
  echo "!! 步骤【$STEP】失败，回滚已变更的系统配置"
  for f in /etc/systemd/system/${SVC}.service /etc/nginx/sites-available/yunying; do
    if [ -f "$f.bak.$TS" ]; then mv -f "$f.bak.$TS" "$f"; echo "已还原 $f"; fi
  done
  systemctl daemon-reload 2>/dev/null || true
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
  echo "回滚完成。请根据上方报错排查后重试（脚本可重复执行）。"
  exit 1
}
trap rollback ERR
step() { STEP="$1"; echo "== $1 =="; }

step "1/7 系统包（幂等）"
NEED=()
dpkg -s python3-venv  >/dev/null 2>&1 || NEED+=(python3-venv)
dpkg -s nginx         >/dev/null 2>&1 || NEED+=(nginx)
dpkg -s curl          >/dev/null 2>&1 || NEED+=(curl)
if [ ${#NEED[@]} -gt 0 ]; then apt-get update -qq && apt-get install -y -qq "${NEED[@]}"; else echo "已安装，跳过"; fi

step "2/7 Python 版本检查"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
  || { echo "需要 Python >= 3.11（当前 $(python3 -V)）"; exit 1; }

step "3/7 目录与代码"
mkdir -p $APP_DIR/app $APP_DIR/web $APP_DIR/data/thumbs $APP_DIR/models $APP_DIR/venv
cp -f demo/serve_demo.py demo/query_translate.py demo/requirements.txt $APP_DIR/app/
cp -f demo/web/index.html $APP_DIR/web/
if [ -f data/demo/embeddings.npy ]; then
  cp -f data/demo/embeddings.npy data/demo/meta.json $APP_DIR/data/
  cp -f data/demo/thumbs/*.webp $APP_DIR/data/thumbs/ 2>/dev/null || true
else
  echo "提示：样例向量 data/demo/embeddings.npy 未找到，请先从仓库 data/demo/ 上传"
fi

step "4/7 venv 与依赖（幂等）"
if [ ! -x $APP_DIR/venv/bin/pip ]; then python3 -m venv $APP_DIR/venv; fi
$APP_DIR/venv/bin/pip install -q --upgrade pip
$APP_DIR/venv/bin/pip install -q -r $APP_DIR/app/requirements.txt
$APP_DIR/venv/bin/python -c "import onnxruntime,numpy,tokenizers,fastapi; print('deps ok, ort', onnxruntime.__version__)"
$APP_DIR/venv/bin/python - <<'PY'
import onnxruntime as o
major, minor = o.__version__.split(".")[:2]
if int(major) < 1 or (int(major) == 1 and int(minor) < 31):
    raise SystemExit("onnxruntime 必须 >= 1.31.0（1.20 读不了 prepacked 外部数据）")
print("ort 版本检查通过")
PY

step "5/7 模型文件检查"
for f in model.onnx model.data tokenizer.json; do
  [ -s $APP_DIR/models/$f ] || { echo "缺少 $APP_DIR/models/$f —— 请按 readme/01 第 3 节从 immich_machine_learning 容器导出"; exit 1; }
done
echo "模型三件套就绪：$(du -sh $APP_DIR/models | cut -f1)"

step "6/7 systemd（幂等 + 备份回滚）"
cat > $APP_DIR/yunying-demo.service <<UNIT
[Unit]
Description=YunYing semantic search demo
After=network.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/app/serve_demo.py
Restart=always
MemoryMax=1600M
Environment=MODEL_DIR=$APP_DIR/models

[Install]
WantedBy=multi-user.target
UNIT
if [ -f /etc/systemd/system/${SVC}.service ]; then cp /etc/systemd/system/${SVC}.service /etc/systemd/system/${SVC}.service.bak.$TS; fi
cp -f $APP_DIR/yunying-demo.service /etc/systemd/system/${SVC}.service
systemctl daemon-reload && systemctl enable --now ${SVC}
sleep 3
systemctl is-active ${SVC} || { journalctl -u ${SVC} -n 30 --no-pager; exit 1; }

step "7/7 nginx（幂等 + 备份回滚）"
NG=/etc/nginx/sites-available/yunying
cat > /tmp/yunying.nginx <<NGINX
server {
    listen 80 default_server;
    root $APP_DIR/web;
    index index.html;

    location /api/ {
        proxy_pass http://127.0.0.1:8100/api/;
        proxy_set_header Host \$host;
    }
    location /thumbs/ {
        alias $APP_DIR/data/thumbs/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
    gzip on;
    gzip_types text/html application/json image/svg+xml;
}
NGINX
[ -f $NG ] && cp $NG $NG.bak.$TS
cp -f /tmp/yunying.nginx $NG
ln -sf $NG /etc/nginx/sites-enabled/yunying
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

step "自检"
curl -s http://127.0.0.1/api/health && echo
echo "安装完成 → http://$(hostname -I | awk '{print $1}')"