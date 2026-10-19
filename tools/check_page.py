import re, subprocess, sys, tempfile, os, urllib.request
html = urllib.request.urlopen("http://106.52.116.164/", timeout=30).read().decode()
scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
print("inline scripts:", len(scripts))
for i, s in enumerate(scripts):
    p = os.path.join(tempfile.gettempdir(), "page_check_%d.js" % i)
    open(p, "w", encoding="utf-8").write(s)
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
    print("script %d: %s %s" % (i, "OK" if r.returncode == 0 else "FAIL", r.stderr.strip()[:300]))
print("has chips container:", 'id="chips"' in html, "| has api call:", "/api/search" in html)
