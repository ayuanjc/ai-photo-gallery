# 启用 / 停用 dsh-server-monitor（改完必须重启 DSH Desktop 才生效）
param([ValidateSet('on','off','status')][string]$Action = 'status')

$prof = Join-Path $env:USERPROFILE '.dsh\profiles\desktop'
$pj   = Join-Path $prof 'package.json'
$dir  = Join-Path $prof 'node_modules\dsh-server-monitor'
$name = 'dsh-server-monitor'

function Read-Profile { Get-Content $pj -Raw | ConvertFrom-Json }

switch ($Action) {
  'status' {
    $j = Read-Profile
    $inBundles = @($j.dsh.profile.bundles) -contains $name
    $hasDir = Test-Path (Join-Path $dir 'lib\client.js')
    $dep = $j.dependencies.$name
    "bundles 含插件 : $inBundles"
    "插件目录存在   : $hasDir"
    "dependencies   : $dep"
    if (-not $inBundles) { ' => 当前不生效；启用请跑: .\tools\toggle-server-monitor.ps1 on' }
    else { ' => 已启用；重启 DSH Desktop 后，侧栏「上下文洞察」下方会出现「服务器」' }
  }
  'on' {
    $j = Read-Profile
    $b = @($j.dsh.profile.bundles)
    if ($b -notcontains $name) { $b += $name }
    $j.dsh.profile.bundles = $b
    if (-not $j.dependencies.$name) { $j.dependencies | Add-Member -NotePropertyName $name -NotePropertyValue 'file:C:/Users/ayuan/IDEProjects/a01/tools/dsh-server-monitor' }
    ($j | ConvertTo-Json -Depth 20) | Set-Content $pj -Encoding utf8
    'enabled. 重启 DSH Desktop 生效。'
  }
  'off' {
    $j = Read-Profile
    $j.dsh.profile.bundles = @($j.dsh.profile.bundles) | Where-Object { $_ -ne $name }
    ($j | ConvertTo-Json -Depth 20) | Set-Content $pj -Encoding utf8
    'disabled. 重启 DSH Desktop 生效。插件目录未删除，随时可再 on。'
  }
}
