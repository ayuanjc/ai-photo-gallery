# 本机 SSH 隧道：127.0.0.1:8123 -> 106.52.116.164:80
# 为什么需要：本机系统代理（HTTP_PROXY=127.0.0.1:7891）经常没在跑，浏览器访问公网 IP 会 Failed to fetch；
# 而 127.0.0.1 在系统代理 bypass 列表里，走隧道不受代理开关影响。
param([ValidateSet('start','stop','status','install')][string]$Action = 'status')

$LocalPort = 8123
$Remote = '106.52.116.164:80'
$Target = 'root@106.52.116.164'

function Test-Port {
  return [bool](Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue)
}

switch ($Action) {
  'status' {
    $up = Test-Port
    "listen 127.0.0.1:$LocalPort : $up"
    if ($up) {
      try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$LocalPort/monitor/api/monitor/now" -UseBasicParsing -TimeoutSec 5
        "health: $($r.StatusCode) $($r.Content)"
      } catch { "health: FAIL $($_.Exception.Message)" }
    }
  }
  'start' {
    if (Test-Port) { "already listening on $LocalPort"; break }
    Start-Process -FilePath 'ssh' -WindowStyle Hidden -ArgumentList @(
      '-N','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes',
      '-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
      '-L',"127.0.0.1:$LocalPort`:$Remote",$Target
    )
    Start-Sleep -Seconds 3
    "started: listen=$((Test-Port))"
  }
  'stop' {
    $c = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { Stop-Process -Id $c.OwningProcess -Force; "stopped pid $($c.OwningProcess)" } else { 'not running' }
  }
  'install' {
    # 登录时自动拉起（可选）
    $ps = (Get-Process -Id $PID).Path
    $cmd = "`"$ps`" -NoProfile -WindowStyle Hidden -File `"$PSCommandPath`" start"
    schtasks /Create /TN 'dsh-monitor-tunnel' /SC ONLOGON /RL LIMITED /TR $cmd /F | Out-Null
    'installed scheduled task dsh-monitor-tunnel (ONLOGON). remove with: schtasks /Delete /TN dsh-monitor-tunnel /F'
  }
}
