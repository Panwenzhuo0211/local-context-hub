param([Parameter(Mandatory=$true)][ValidateSet('codex','zcode')][string]$Adapter)
$ErrorActionPreference='Stop'
$bridgeConfig = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'desktop-launch.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$app = $bridgeConfig.$Adapter
if (-not (Test-Path -LiteralPath $app.executable)) { throw '客户端路径已变化，请重新生成桥接启动器。' }
$active = Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $app.executable }
if ($active) {
    Write-Host '请先退出该客户端，再运行此启动器。已有进程不会读取新的桥接配置。' -ForegroundColor Yellow
    exit 2
}
if ($Adapter -eq 'codex') {
    Push-Location (Split-Path $PSScriptRoot -Parent)
    try {
        & $bridgeConfig.python -X utf8 -m context_hub.collision_repair --data-dir $bridgeConfig.hub
        if ($LASTEXITCODE -ne 0) { throw '原生记录修复未完成，已暂停启动，避免继续写入冲突记录。' }
    } finally { Pop-Location }
}
$previous = @{}
# A worker started directly under the desktop can be killed with its Windows
# process job. Wake the existing independent scheduler task on every launch.
$archiveTask = Get-ScheduledTask -TaskName 'dev.local.context-hub' -ErrorAction SilentlyContinue
if ($archiveTask) { Start-ScheduledTask -InputObject $archiveTask }
try {
    foreach($entry in $app.environment.PSObject.Properties) {
        $previous[$entry.Name]=[Environment]::GetEnvironmentVariable($entry.Name,'Process')
        [Environment]::SetEnvironmentVariable($entry.Name,$entry.Value,'Process')
    }
    $startOptions = @{FilePath=$app.executable;WindowStyle='Normal'}
    if ($app.workingDirectory) { $startOptions.WorkingDirectory=$app.workingDirectory }
    Start-Process @startOptions
} finally {
    foreach($name in $previous.Keys) {[Environment]::SetEnvironmentVariable($name,$previous[$name],'Process')}
}
