#!/usr/bin/env python3
import os, time, subprocess, shutil

CONF = '/etc/nginx/sites-enabled/zhihui-live.conf'
DENY = '/etc/nginx/conf.d/00-default-deny.conf'
NEWNAME = '    server_name zcstzhihui.icu www.zcstzhihui.icu zcstzhihui.eu.cc;'

src = open(CONF, encoding='utf-8').read()
os.makedirs('/srv/backup/nginx', exist_ok=True)
bak = '/srv/backup/nginx/zhihui-live.conf.%s' % time.strftime('%Y%m%d-%H%M%S')
shutil.copy2(CONF, bak)
print('backup ->', bak)

out = src.replace('# 智汇摄影协会官网 —— WordPress 生产站点（8.163.74.227）',
                  '# 智汇摄影协会官网 —— WordPress 生产站点（154.94.227.106）')

for a, b in [('    listen 80 default_server;\n', '    listen 80;\n'),
             ('    listen [::]:80 default_server;\n', '    listen [::]:80;\n')]:
    assert out.count(a) == 1, 'expect 1 of ' + a.strip() + ', got %d' % out.count(a)
    out = out.replace(a, b)

for a in ['    server_name zcstzhihui.eu.cc zcstzhihui.eu.cc zcstzhihui.icu www.zcstzhihui.icu 8.163.74.227 _;\n',
          '    server_name zcstzhihui.eu.cc zcstzhihui.eu.cc zcstzhihui.icu www.zcstzhihui.icu 8.163.74.227;\n']:
    assert a in out, 'server_name pattern missing'
    out = out.replace(a, NEWNAME, 1)

bad = [l for l in out.splitlines() if 'server_name' in l and ('8.163.74.227' in l or ' _;' in l)]
assert not bad, 'leftover: ' + repr(bad)
print('server_name normalised, stale IP only in comment now:', '8.163.74.227' in out)

deny = '''# 2026-11-20: 直连兜底。站点只认域名 Host；裸 IP / 未知 Host 一律 444 断开，
# 避免扫描器在源站 IP 上直接逛站（B 路线：保持直连，不做 CDN）。
# 443 兜底复用主站证书，无 SNI 的老客户端握手仍能成功，
# 之后按 Host 头判定：Host 是本站域名 -> 命中正常 server block，不受影响。
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    access_log off;
    return 444;
}

server {
    listen 443 ssl http2 default_server;
    listen [::]:443 ssl default_server;
    server_name _;
    ssl_certificate     /etc/ssl/zhihui/le-fullchain.pem;
    ssl_certificate_key /etc/ssl/zhihui/le-privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    access_log off;
    return 444;
}
'''
with open('/tmp/00-default-deny.conf', 'w', encoding='utf-8', newline='') as f:
    f.write(deny)
with open('/tmp/zhihui-live.new', 'w', encoding='utf-8', newline='') as f:
    f.write(out)
assert os.path.getsize('/tmp/zhihui-live.new') > 10000
os.replace('/tmp/zhihui-live.new', CONF)
os.replace('/tmp/00-default-deny.conf', DENY)
print('written')

t = subprocess.run(['nginx', '-t'], capture_output=True, text=True)
print('nginx -t:', t.returncode, '|', (t.stderr or t.stdout).strip().replace('\n', ' | ')[:400])
if t.returncode != 0:
    shutil.copy2(bak, CONF)
    os.remove(DENY)
    print('ROLLED BACK')
    raise SystemExit(1)
subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
print('nginx reloaded')
