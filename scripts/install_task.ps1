# 建立 Windows 排程工作 "CCUsageRelay"：每 5 分鐘以 pythonw 背景執行 agent.py
$ErrorActionPreference = "Stop"

$repoRoot  = Split-Path -Parent $PSScriptRoot
$agentPath = Join-Path $repoRoot "agent\agent.py"
$configPath = Join-Path $repoRoot "agent\config.json"

if (-not (Test-Path $agentPath)) {
    Write-Error "找不到 $agentPath"
    exit 1
}
if (-not (Test-Path $configPath)) {
    Write-Warning "agent\config.json 不存在 — 請先複製 config.json.example 並填值，否則排程會持續失敗。"
}

# 偵測 pythonw（無視窗），找不到改用 python
$py = Get-Command pythonw -ErrorAction SilentlyContinue
if ($null -eq $py) {
    $py = Get-Command python -ErrorAction SilentlyContinue
}
if ($null -eq $py) {
    Write-Error "找不到 pythonw 或 python，請先安裝 Python 3.11+ 並加入 PATH。"
    exit 1
}
$pyExe = $py.Source
Write-Host "使用直譯器: $pyExe"

# \" 讓 schtasks 收到帶引號的完整 /TR 值（路徑含空白也安全）
$taskRun = '\"{0}\" \"{1}\"' -f $pyExe, $agentPath

schtasks /Create /TN "CCUsageRelay" /SC MINUTE /MO 5 /RL LIMITED /F /TR $taskRun
if ($LASTEXITCODE -ne 0) {
    Write-Error "schtasks 建立失敗（exit $LASTEXITCODE）"
    exit 1
}

Write-Host "排程已建立，立即觸發一次..."
schtasks /Run /TN "CCUsageRelay" | Out-Null

Write-Host ""
Write-Host "完成。約 10 秒後可查看執行紀錄："
Write-Host "  Get-Content `"$repoRoot\agent\agent.log`" -Tail 20"
