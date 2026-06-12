# 移除排程工作 "CCUsageRelay"
schtasks /Delete /TN "CCUsageRelay" /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "已移除排程 CCUsageRelay。"
} else {
    Write-Warning "移除失敗或排程不存在（exit $LASTEXITCODE）。"
}
