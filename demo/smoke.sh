#!/usr/bin/env bash
# 服务器端自检：健康 + 三组查询的耗时与命中质量
set -u
B=http://127.0.0.1
echo "--- health"
curl -s $B/api/health; echo
echo "--- thumbs 抽样"
curl -s -o /dev/null -w "thumb %{http_code} %{size_download}B %{time_total}s\n" "$B/thumbs/$(ls /opt/yunying/data/thumbs | head -1)"
echo "--- 搜索：中文 / 英文 / 纯场景词"
for Q in "海边日落" "城市夜景" "a dog" "火锅" "蓝色的车"; do
  curl -s -X POST $B/api/search -H 'Content-Type: application/json' \
    -d "{\"query\":\"$Q\",\"top_k\":6}" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("Q=%s -> %s (%s) %sms" % (d["query"], d["translated"][:70], "改写" if d["translated_used"] else "原样", d["timing_ms"]["total"]));print("   ", [(r["score"], (r["scene"] or "-")) for r in d["results"][:6]])'
done
echo "--- 内存 / 进程"
systemctl is-active yunying-demo
ps -o rss= -C python3 2>/dev/null | awk '{s+=$1} END {print "python rss MB:", s/1024}'
free -m | head -2
