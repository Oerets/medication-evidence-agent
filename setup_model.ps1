$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$archivePath = Join-Path $PSScriptRoot 'work\runtime\ollama.zip'
$runtimeDir = Join-Path $PSScriptRoot 'work\runtime\ollama'
New-Item -ItemType Directory -Force -Path (Split-Path $archivePath), (Join-Path $PSScriptRoot 'data\models') | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir 'ollama.exe'))) {
    curl.exe -L --fail --silent --show-error -o $archivePath https://github.com/ollama/ollama/releases/download/v0.40.2/ollama-windows-amd64.zip
    if ($LASTEXITCODE -ne 0) { throw 'Runtime download failed' }
    $archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLower()
    if ($archiveHash -ne 'e29ad1d5063dd4b54b2492d9b00adab2cff9621bfa654b6c92aa8d6f1fdfe7fc') { throw 'Runtime checksum mismatch' }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $runtimeDir -Force
}
& (Join-Path $PSScriptRoot 'start_model.ps1')
for ($attempt = 0; $attempt -lt 15; $attempt++) {
    try { Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep -Seconds 1 }
}
& (Join-Path $runtimeDir 'ollama.exe') pull qwen3:32b
if ($LASTEXITCODE -ne 0) { throw 'Model download failed' }
