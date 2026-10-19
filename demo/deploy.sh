#!/usr/bin/env bash
# 云影图鉴 · 轻量在线 demo 部署（在本地 Windows/Git Bash 或服务器上均可读）
set -euo pipefail

HOST=${HOST:-root@106.52.116.164}
REMOTE=/opt/yunying

echo '== 1/5 上传应用文件 =='
ssh $HOST "mkdir -p $REMOTE/app $REMOTE/web $REMOTE/data/thumbs"
scp demo/serve_demo.py demo/query_translate.py demo/requirements.txt $HOST:$REMOTE/app/
scp demo/web/index.html $HOST:$REMOTE/web/
scp data/demo/embeddings.npy data/demo/meta.json $HOST:$REMOTE/data/
scp -r data/demo/thumbs/. $HOST:$REMOTE/data/thumbs/

echo '== 2/5 建 venv 装依赖 =='
ssh $HOST 'set -e; cd $REMOTE; python3 -m venv venv 2>/dev/null || true; ./venv/bin/pip install -q --upgrade pip; ./venv/bin/pip install -q -r app/requirements.txt; ./venv/bin/python -c "import onnxruntime,numpy,tokenizers,fastapi;print(\"deps ok\")"'

echo '== 3/5 装 systemd =='
scp demo/yunying-demo.service $HOST:/etc/systemd/system/
ssh $HOST 'systemctl daemon-reload && systemctl enable --now yunying-demo && sleep 3 && systemctl is-active yunying-demo'

echo '== 4/5 装 nginx =='
scp demo/nginx.demo.conf $HOST:/etc/nginx/sites-available/yunying
ssh $HOST 'ln -sf /etc/nginx/sites-available/yunying /etc/nginx/sites-enabled/yunying; rm -f /etc/nginx/sites-enabled/default; nginx -t && systemctl reload nginx'

echo '== 5/5 自检 =='
ssh $HOST 'curl -s localhost/api/health; echo; curl -s -o /dev/null -w "index %{http_code} %{size_download}B\n" localhost/'
echo '部署完成 → http://106.52.116.164'
