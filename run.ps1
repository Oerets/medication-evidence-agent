$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& (Join-Path $PSScriptRoot 'start_model.ps1')
python -m app.data
if ($LASTEXITCODE -ne 0) { throw '데이터 적재 실패' }
python -m app.server
