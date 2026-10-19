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

echo '== 3/5 装模型目录（兼容迁移） + systemd =='
# 统一模型路径约定为 /opt/yunying/models（与 readme/install.sh 一致）：
# 旧部署的 /opt/yunying/clip 若存在且 models 不存在则原位迁移
ssh $HOST "if [ -d $REMOTE/clip ] && [ ! -d $REMOTE/models ]; then mv $REMOTE/clip $REMOTE/models && echo 'moved clip -> models'; fi"
scp demo/yunying-demo.service $HOST:/etc/systemd/system/
# enable + restart：enable --now 对已运行服务是 no-op，更新部署必须 restart 才生效
ssh $HOST 'systemctl daemon-reload && systemctl enable yunying-demo && systemctl restart yunying-demo && sleep 3 && systemctl is-active yunying-demo'

echo '== 4/5 装 nginx =='
scp demo/nginx.demo.conf $HOST:/etc/nginx/sites-available/yunying
ssh $HOST 'ln -sf /etc/nginx/sites-available/yunying /etc/nginx/sites-enabled/yunying; rm -f /etc/nginx/sites-enabled/default; nginx -t && systemctl reload nginx'

echo '== 5/5 自检 =='
# -f：后端 5xx 时 curl 以非零退出，部署脚本在此失败而不是误报成功
ssh $HOST 'set -e; curl -fsS --max-time 10 localhost/api/health; echo; code=$(curl -s -o /dev/null -w "%{http_code}" localhost/); echo "index $code"; [ "$code" = "200" ]'
echo '部署完成 → http://106.52.116.164'
