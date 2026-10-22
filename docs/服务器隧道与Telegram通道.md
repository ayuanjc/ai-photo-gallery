# 服务器隧道与 Telegram 通道

> 一句话：服务器在国内、Telegram 在外面，中间那条路靠你本机的 Clash 借道过去。本文记录这条路怎么搭的、怎么维护、断了怎么查。

---

## 一、为什么需要它

服务器 `106.52.116.164`（腾讯云广州）出网能力实测：

| 目标 | 结果 |
|---|---|
| `api.telegram.org` | 000 / 8.0s 超时 |
| `google.com` | 000 / 8.0s 超时 |
| `baidu.com` | 200 / 0.055s |
| IPv6 出口 | 无 |

先在服务器上装 mihomo 直连机场节点，也失败了：节点端口从腾讯云出去 `dial tcp 216.23.108.78:46244: i/o timeout`，DNS 是干净的（本机与服务器解析同一 IP），纯粹是**机房出口到节点的链路不通**。

结论：服务器自己出不去。唯一可行的是**借你本机的网络**——你本机能出网（`api.telegram.org` 302 / 1.8s），把它的代理端口反向映射到服务器上。

---

## 二、架构

```
PocketPaw（服务器 127.0.0.1:8888）
   │  HTTP_PROXY=http://127.0.0.1:7892
   ▼
服务器 127.0.0.1:7892   ←── ssh -R 反向隧道 ──┐
                                            │
                             你本机 127.0.0.1:7891（Clash / mihomo）
                                            │
                                            ▼
                                     api.telegram.org
```

三层各管一件事：

1. **本机 mihomo**（`127.0.0.1:7891`）负责真正出网，端口跟着 Clash Verge 的 `mixed-port` 走。
2. **ssh 反向隧道**把服务器的 `7892` 接到本机的 `7891`，方向是「服务器发起的连接 → 本机代理」。
3. **PocketPaw** 只认 `127.0.0.1:7892`，不关心外面世界怎么变。

---

## 三、组件清单

| 位置 | 文件 / 单元 | 作用 |
|---|---|---|
| 本机 | `tools/ssh_tunnel.ps1` | 守护脚本：等 Clash 起来 → 建隧道 → 断了 5 秒重连 |
| 本机 | 计划任务 `YunyingSSHTunnel` | 登录即启动上面的脚本，永不超时，失败每分钟重试 |
| 本机 | `tools/ssh_tunnel.log` | 运行日志，超 1 MB 自动清空重写 |
| 服务器 | `/opt/mihomo/` | 之前装的直连方案，现在只作备份，**没在用** |
| 服务器 | `systemd: pocketpaw` | 服务本体，监听 `127.0.0.1:8888` |
| 服务器 | `/root/.pocketpaw/config.json` | `telegram_bot_token` / `allowed_user_id=7438804564` / `channel_autostart.telegram=true` |

脚本关键参数（改端口就改这三行）：

```powershell
$SshTarget  = "root@106.52.116.164"
$RemotePort = 7892    # 服务器上监听的端口
$LocalPort  = 7891    # 本机 Clash 的 mixed-port
```

---

## 四、日常运维

### 看状态

```powershell
# 本机：任务在不在跑
Get-ScheduledTask -TaskName YunyingSSHTunnel | Select-Object State
Get-NetTCPConnection -State Listen -LocalPort 7891,7892 -ErrorAction SilentlyContinue
Get-Content tools\ssh_tunnel.log -Tail 20
```

```bash
# 服务器：隧道通不通 + 服务活着没
ss -ltn | grep 7892
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' -x http://127.0.0.1:7892 https://api.telegram.org
systemctl is-active pocketpaw
curl -s http://127.0.0.1:8888/api/telegram/status
```

正常的样子：`302` + 1 秒内、`active`、`{"configured":true,"user_id":7438804564}`。

### 停 / 启 / 重来

```powershell
Stop-ScheduledTask  -TaskName YunyingSSHTunnel
Start-ScheduledTask -TaskName YunyingSSHTunnel
# 彻底删掉
Unregister-ScheduledTask -TaskName YunyingSSHTunnel -Confirm:$false
```

### 换端口

改 `ssh_tunnel.ps1` 里的 `$RemotePort`，同步改服务器 PocketPaw 的 `HTTP_PROXY`，再 `systemctl restart pocketpaw`。

---

## 五、断了怎么查（按顺序）

| 现象 | 先查 | 结论 |
|---|---|---|
| 服务器 `curl -x 7892` 超时 | `ss -ltn \| grep 7892` 有没有 | 没有 → 隧道没建，看本机任务状态 |
| 服务器 7892 在，一访问就断 | 本机 `Get-NetTCPConnection -LocalPort 7891` | 没有 → Clash 没开或换了端口 |
| 本机能连 7891 但页面打不开 | Clash 里节点是否可用 | 节点挂了 → 换节点，隧道不用动 |
| 隧道正常、PocketPaw 仍报 `ConnectError` | `journalctl -u pocketpaw -n 30` 时间戳 | 错误时间早于隧道建立 → 历史残留，重启服务 |
| 两边都正常，bot 不回话 | Telegram 里是否 `/start` 过、`allowed_user_id` 对不对 | ID 不匹配 → 改 config 重启 |

日志里的两条典型记录：

```
2026-10-21 14:54:55 supervisor started (pid=51252)
2026-10-21 14:54:56 connecting -R 7892:127.0.0.1:7891 -> root@106.52.116.164
```

只出现第一条、没有第二条 → Clash 没起，脚本在 30 秒一轮地等。

---

## 六、已知限制

1. **本机必须开机且已登录**——触发器是 `AtLogOn`，锁屏没事，注销就断。
2. **Clash 必须开着**——脚本只检查 7891 是否监听，不检查节点是否可用。
3. **Clash 改端口会断**——`mixed-port` 换成别的值时，改脚本 `$LocalPort` 再重启任务。
4. **这是借道，不是翻墙方案**——只服务 PocketPaw 这一个本地端口，`allow-lan` 关闭，没有对外暴露。
5. **服务器上那套 mihomo 是死路**——`/opt/mihomo/` 留着但 `systemctl is-enabled mihomo` 可以关掉，省 60 MB 内存。

---

## 七、换机器怎么重建

本机重装 / 换电脑时按这个顺序：

1. 新机器的 `%USERPROFILE%\.ssh\` 放好私钥（旧机器上的 `id_ed25519` 或 `DSH.pem`），确认 `ssh root@106.52.116.164` 能直连。
2. 装 Clash 并登录，确认 `127.0.0.1:7891` 在监听、能打开 `api.telegram.org`。
3. 拷 `tools/ssh_tunnel.ps1`，按新机器的 Clash 端口改 `$LocalPort`。
4. 重新注册计划任务（`New-ScheduledTaskAction` + `-AtLogOn` + `-ExecutionTimeLimit 0`）。
5. 服务器侧不用动。

服务器重装时才需要重建 `pocketpaw` 服务和 `/root/.pocketpaw/config.json`。
