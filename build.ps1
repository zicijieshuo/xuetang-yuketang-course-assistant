param([switch]$SkipTests)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python environment' }
}
& $taskPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
if (-not $SkipTests) {
    & $taskPython -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
}
& $taskPython -m PyInstaller --noconfirm assistant.spec
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
Write-Host 'Build complete. See dist\XuetangAssistant.'
