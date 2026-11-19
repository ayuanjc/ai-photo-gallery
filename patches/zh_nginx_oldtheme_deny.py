#!/usr/bin/env python3
# 2026-11-20 夜间巡检补丁：旧主题目录 zhihui.old / zhihui.prev.NNNN 仍在 web 根下，
# 且 PHP 可被直接请求执行（实测 functions.php 200、page-submit.php 500）。
# 旧模板不受当前主题的修补覆盖，等于把历史版本的攻击面一直挂在公网。
import os, shutil, sys, time

P = '/etc/nginx/sites-enabled/zhihui-live.conf'
ANCHOR = '    location ^~ /wp-content/uploads/ {'
MARK = 'zhihui-2026-11-20 oldtheme-deny'
ADD = ('    # ' + MARK + '：旧主题目录整目录拒绝外网（不能删——是用户的历史备份）\n'
       '    location ~* ^/wp-content/themes/zhihui\\.(old|prev\\.[0-9]+)(/|$) {\n'
       '        deny all;\n'
       '    }\n\n')

src = open(P, mode='r', encoding='utf-8').read()
if MARK in src:
    print('already patched'); sys.exit(0)

n = src.count(ANCHOR)
assert n == 2, 'anchor count %d (expected 2 server blocks)' % n

ts = time.strftime('%Y%m%d-%H%M%S')
d = '/srv/backup/nginx'
os.makedirs(d, exist_ok=True)
bak = os.path.join(d, 'zhihui-live.conf.' + ts)
shutil.copy2(P, bak)

new = src.replace(ANCHOR, ADD + ANCHOR)
assert new.count(MARK) == 2, 'marker not applied twice'
assert len(new) > len(src)

tmp = P + '.zh-20261120.tmp'
with open(tmp, mode='w', encoding='utf-8', newline='') as f:
    f.write(new)
os.replace(tmp, P)
print('patched bytes %d -> %d  backup=%s' % (len(src), len(new), bak))
