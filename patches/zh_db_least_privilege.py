#!/usr/bin/env python3
# 2026-11-20: DB least privilege for WordPress user.
import os, subprocess, time

DB = 'zhihui_live'
USER = 'zhihui'
HOSTS = ['localhost', '127.0.0.1']
KEEP = ('SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, '
        'CREATE TEMPORARY TABLES, LOCK TABLES, REFERENCES')
MARK = 'ON ' + DB + '.*'

def sql(q):
    return subprocess.run(['mysql', '-uroot', '-N', '-e', q],
                          capture_output=True, text=True).stdout.strip()

os.makedirs('/srv/backup/mysql', exist_ok=True)
ts = time.strftime('%Y%m%d-%H%M%S')
bak = '/srv/backup/mysql/grant-restore-zhihui.%s.sql' % ts

lines = ['-- rollback: mysql -uroot < ' + bak + ' ;']
for h in HOSTS:
    for g in sql("SHOW GRANTS FOR '%s'@'%s'" % (USER, h)).splitlines():
        if MARK in g:
            lines.append(g.rstrip(';') + ';')
with open(bak, 'w', encoding='utf-8', newline='') as f:
    f.write('\n'.join(lines) + '\n')
print('backup ->', bak)
print(open(bak).read().strip())

for h in HOSTS:
    for q in ("REVOKE ALL PRIVILEGES, GRANT OPTION FROM '%s'@'%s'" % (USER, h),
              "GRANT %s ON %s.* TO '%s'@'%s'" % (KEEP, DB, USER, h)):
        r = subprocess.run(['mysql', '-uroot', '-e', q], capture_output=True, text=True)
        print(('OK  ' if r.returncode == 0 else 'ERR ') + q[:72] + ((' :: ' + r.stderr.strip()) if r.returncode else ''))

print('--- after ---')
for h in HOSTS:
    print(sql("SHOW GRANTS FOR '%s'@'%s'" % (USER, h)))
