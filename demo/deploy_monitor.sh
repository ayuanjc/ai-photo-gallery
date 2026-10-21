set -e
say(){ echo "[deploy] $*"; }
H=/opt/yunying/monitor

say "1) dir + deps"
mkdir -p $H
/opt/yunying/venv/bin/pip install -q --disable-pip-version-check -i https://mirrors.tencentyun.com/pypi/simple psutil==7.2.2
/opt/yunying/venv/bin/python -c "import psutil;print('  psutil', psutil.__version__)"
ls -l $H

say "2) systemd unit"
cat > /etc/systemd/system/yunying-monitor.service <<'UNIT'
[Unit]
Description=Yunying realtime resource monitor
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/yunying/monitor
Environment=MON_INTERVAL=2
Environment=MON_HISTORY=900
Environment=MON_SERVICES=nginx,yunying-demo
ExecStart=/opt/yunying/venv/bin/uvicorn monitor:app --app-dir /opt/yunying/monitor --host 127.0.0.1 --port 8101 --workers 1
Restart=always
RestartSec=3
MemoryMax=256M

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable yunying-monitor >/dev/null 2>&1 || true
systemctl restart yunying-monitor
sleep 4
echo "  active=$(systemctl is-active yunying-monitor)"

say "3) nginx"
cp /etc/nginx/sites-available/yunying /root/yunying.nginx.bak.$(date +%s)
cat > /etc/nginx/sites-available/yunying <<'CONF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    root /opt/yunying/web;
    index index.html;

    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;
    gzip_min_length 512;

    location /thumbs/ {
        alias /opt/yunying/data/thumbs/;
        expires 30d;
        add_header Cache-Control "public, max-age=2592000, immutable";
        access_log off;
    }

    location = /monitor {
        return 301 /monitor/;
    }

    location /monitor/ {
        proxy_pass http://127.0.0.1:8101/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
        chunked_transfer_encoding on;
        gzip off;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8100;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 60s;
    }

    location / { try_files $uri $uri/ /index.html; }
}
CONF
nginx -t 2>&1 | tail -2
systemctl reload nginx

say "4) self-check"
curl -s -m 5 http://127.0.0.1:8101/api/monitor/now | head -c 260; echo
curl -s -o /dev/null -w "  page=%{http_code} bytes=%{size_download}\n" http://127.0.0.1/monitor/
echo -n "  sse: "; timeout 6 curl -s -N http://127.0.0.1/monitor/api/monitor/stream | grep -c '^data: ' || true
say "done"
