import json, urllib.request, time, statistics, concurrent.futures as cf
B="http://106.52.116.164"
def post(q,k=24):
    req=urllib.request.Request(B+"/api/search",data=json.dumps({"query":q,"top_k":k}).encode(),headers={"Content-Type":"application/json"})
    t=time.time(); d=json.load(urllib.request.urlopen(req,timeout=30)); return d,(time.time()-t)*1000
d,ms1=post("海边日落"); print("1st  %6.1fms %s cached=%s" % (ms1, d["timing_ms"], d["text_cached"]))
d,ms2=post("海边日落"); print("2nd  %6.1fms %s cached=%s" % (ms2, d["timing_ms"], d["text_cached"]))
def one(i):
    q=["雪山","蓝色的车","城市夜景","烟花","森林","猫","湖","火锅","街边小吃","人物肖像"][i]
    return post(q)[1]
with cf.ThreadPoolExecutor(10) as ex: ts=list(ex.map(one,range(10)))
print("10 并发(不同查询): 平均 %.0fms 最大 %.0fms" % (statistics.mean(ts), max(ts)))
with cf.ThreadPoolExecutor(10) as ex: ts=list(ex.map(lambda _: post("海边日落")[1], range(10)))
print("10 并发(同一查询,命中缓存): 平均 %.0fms 最大 %.0fms" % (statistics.mean(ts), max(ts)))
