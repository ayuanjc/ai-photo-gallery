#!/usr/bin/env python3
# 2026-11-20 夜间巡检：sshd 加固（第二版：只拒绝真正的顶层 Match 块）
import os, re, shutil, sys, time

P = '/etc/ssh/sshd_config'
ts = time.strftime('%Y%m%d-%H%M%S')

src = open(P, encoding='utf-8').read()
lines = src.splitlines(True)

def find(key):
    return [(i, l.rstrip()) for i, l in enumerate(lines) if re.match(r'^%s\s' % key, l, re.I)]

for k in ('PermitRootLogin', 'PasswordAuthentication', 'PubkeyAuthentication', 'MaxAuthTries'):
    print('  %-24s -> %s' % (k, find(k) or 'absent'))

pr, pw, mx = find('PermitRootLogin'), find('PasswordAuthentication'), find('MaxAuthTries')
assert len(pr) == 1, 'PermitRootLogin count %d' % len(pr)
assert len(pw) == 1, 'PasswordAuthentication count %d' % len(pw)
assert len(mx) <= 1, 'MaxAuthTries count %d' % len(mx)
assert not re.search(r'^Match', src, re.M), 'real Match block present, abort'

lines[pr[0][0]] = 'PermitRootLogin prohibit-password\n'
lines[pw[0][0]] = 'PasswordAuthentication no\n'
if mx:
    lines[mx[0][0]] = 'MaxAuthTries 3\n'
else:
    lines.append('MaxAuthTries 3\n')

new = ''.join(lines)
assert new != src
os.makedirs('/srv/backup/ssh', exist_ok=True)
bak = '/srv/backup/ssh/sshd_config.' + ts
shutil.copy2(P, bak)

tmp = P + '.zh-20261120.tmp'
with open(tmp, mode='w', encoding='utf-8', newline='') as f:
    f.write(new)
os.chmod(tmp, 0o600)
os.replace(tmp, P)
print('patched, backup=%s' % bak)
print('effective after reload:')
