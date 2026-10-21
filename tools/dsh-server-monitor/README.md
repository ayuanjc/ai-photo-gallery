# dsh-server-monitor

DSH Web GUI 的服务器监测面板：在左侧面板列表里，「上下文洞察」这一行的**正下方**新增一行「服务器」。

## 它做什么

- 面板行 order = 21（`dsh-context` 是 20），所以紧贴上下文洞察之后。
- 打开后主列显示仪表盘：CPU / 内存 / 系统盘 / 公网吞吐 / 磁盘 IO / 检索服务六张卡，
  一张 CPU% + 内存% 的双折线（最近 60 帧 = 2 分钟），服务状态 chips，运行时长与采样帧数。
- 数据源：`http://106.52.116.164/monitor/api/monitor/{now,history}`，客户端 5 秒轮询；
  页面隐藏时暂停轮询，切回来立即拉一次。
- 页脚有「打开完整面板」链接，跳到带 SSE 实时推送和四张曲线图的独立页。

## 安装

已经装进 `~/.dsh/profiles/desktop`：

```
package.json  dependencies: "dsh-server-monitor": "file:C:/Users/ayuan/IDEProjects/a01/tools/dsh-server-monitor"
              dsh.profile.bundles: 追加 "dsh-server-monitor"
node_modules/dsh-server-monitor/   ← 物理副本（pnpm 未接管时用它）
```

改完**重启 DSH Desktop** 才生效（打包版没有 client HMR，浏览器刷新不够）。

## 端结构

| 文件 | 作用 |
|---|---|
| `lib/index.js` | host 半（Cordis 插件），空实现 —— 面板全在浏览器端 |
| `lib/client.js` | 浏览器半：`window.__ModuleLoader__.load({id, factory})` 形态，含 `apply`/`inject` |
| `cordis.patch.yml` | `insert` 一行，把宿主插件加进 bundle |

客户端注册方式（照官方 slot 座位）：

```js
ctx.slots.inject('sidebar.panellist', () => ctx.slots.register(
  { name: 'sidebar.panellist', id: 'server-monitor', order: 21, label: () => '服务器' },
  MonitorIcon,
))
ctx.slots.inject('main', () => ctx.slots.register(
  { name: 'main', key: 'server-monitor' },
  MonitorPage,
))
```

两条注册都包在 try/catch 里：注册失败只 warn，不会让 web shell 启动失败。

## 依赖的服务端改动

`/opt/yunying/monitor/monitor.py` 末尾加了 CORS 中间件（浏览器从 `127.0.0.1:43120` 跨源取数）：

```python
@app.middleware("http")
async def _cors_headers(request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Max-Age"] = "600"
    return response
```

> 刻意放在后端而不是 nginx：nginx 的 `add_header` 会覆盖同 location 的继承链，且容易和上游头重复（重复的
> `Access-Control-Allow-Origin` 会让浏览器直接拒绝响应）。

## 回滚

```powershell
Copy-Item "$env:USERPROFILE\.dsh\profiles\desktop\package.json.bak-server-monitor" `
          "$env:USERPROFILE\.dsh\profiles\desktop\package.json" -Force
Remove-Item "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\dsh-server-monitor" -Recurse -Force
# 重启 DSH Desktop
```

## 已知限制

1. 端点硬编码为 `106.52.116.164`；换机器改 `ENDPOINT` 一行。
2. 5 秒轮询，非 SSE：浏览器跨源 SSE 要额外开 `EventSource` 头，收益不值得（一帧才 0.2 KB）。
3. 无鉴权：指标是公开只读资源数据（不含进程路径、内网 IP、密钥）。
4. 面板位于主列（点开后占满正文），不是右栏抽屉 —— 右侧栏 tab 走 `sidebar.right.pane.tab` 座位，本项目未使用。
