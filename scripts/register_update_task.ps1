param(
    [string]$TaskName = "RAG-Knowledge-Base-Update",
    [string]$At = "06:00",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$runner = Join-Path $ProjectRoot "scripts\run_update_index.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Не найден runner: $runner"
}

$action = New-ScheduledTaskAction -Execute "PowerShell.exe" -Argument (
    "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -ProjectRoot `"$ProjectRoot`""
)
$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, "HH:mm", $null))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Daily incremental update of the RAG knowledge base" -Force | Out-Null
Write-Output "Задача '$TaskName' зарегистрирована: ежедневно в $At"

