$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtimeExe = Join-Path $PSScriptRoot 'work\runtime\ollama\ollama.exe'
if (-not (Test-Path -LiteralPath $runtimeExe)) { throw 'Ollama runtime missing. Run setup_model.ps1 first.' }
try {
    Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 2 | Out-Null
    Write-Host 'Ollama is already running on 127.0.0.1:11434.'
    return
} catch { }
$env:OLLAMA_MODELS = Join-Path $PSScriptRoot 'data\models'
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_FLASH_ATTENTION = '1'
$env:OLLAMA_KV_CACHE_TYPE = 'q8_0'
$env:OLLAMA_NUM_PARALLEL = '1'
$env:CUDA_VISIBLE_DEVICES = '0'
Start-Process -FilePath $runtimeExe -ArgumentList 'serve' -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'work\runtime\ollama.stdout.log') -RedirectStandardError (Join-Path $PSScriptRoot 'work\runtime\ollama.stderr.log') | Out-Null
Write-Host 'Local Ollama server started.'
