$ErrorActionPreference = 'Stop'
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'work\runtime\ollama')) + '\'
$ownedProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and [IO.Path]::GetFullPath($_.ExecutablePath).StartsWith($runtimeRoot, [StringComparison]::OrdinalIgnoreCase)
}
foreach ($ownedProcess in $ownedProcesses) {
    Stop-Process -Id $ownedProcess.ProcessId -ErrorAction SilentlyContinue
}
Write-Host 'Stopped this workspace Ollama server and inference processes.'
