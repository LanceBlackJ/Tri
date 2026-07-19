$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$projectRoot = Split-Path -Parent $scriptDir
$workspaceRoot = Split-Path -Parent $projectRoot
$venvPython = Join-Path $workspaceRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    $venvPython = "python"
}

Set-Location $projectRoot
$env:PYTHONPATH = $projectRoot
& $venvPython -m celery -A teacher_django worker --pool=solo --loglevel=info