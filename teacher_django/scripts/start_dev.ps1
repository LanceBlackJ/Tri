# 启动开发服务器、任务处理脚本和 Celery worker（Windows PowerShell）
# 用法：在项目根运行： .\teacher_django\scripts\start_dev.ps1

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$projectRoot = Split-Path -Parent $scriptDir
$workspaceRoot = Split-Path -Parent $projectRoot
$venvPython = Join-Path $workspaceRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
	$venvPython = "python"
}

Set-Location $projectRoot

Write-Host "Starting Django development server..."
Start-Process -FilePath "powershell" -ArgumentList "-NoExit","-Command","Set-Location '$projectRoot'; `$env:PYTHONPATH='$projectRoot'; & '$venvPython' .\manage.py runserver"

Write-Host "Starting AgentTask processor..."
Start-Process -FilePath "powershell" -ArgumentList "-NoExit","-Command","Set-Location '$projectRoot'; `$env:PYTHONPATH='$projectRoot'; & '$venvPython' .\manage.py process_agent_tasks --interval 5"

Write-Host "Starting Celery worker..."
Start-Process -FilePath "powershell" -ArgumentList "-NoExit","-Command","Set-Location '$projectRoot'; `$env:PYTHONPATH='$projectRoot'; & '$venvPython' -m celery -A teacher_django worker --pool=solo --loglevel=info"

Write-Host "Started runserver, process_agent_tasks and Celery worker. Redis still needs to be running on 127.0.0.1:6379."
