param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $ProjectRoot

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}

& $python (Join-Path $ProjectRoot "update_index.py") --source (Join-Path $ProjectRoot "knowledge_base") --log (Join-Path $ProjectRoot "logs\index_updates.jsonl")
exit $LASTEXITCODE

