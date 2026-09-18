@echo off
setlocal

title AuraClaw Local Development

cd /d "%~dp0"
if errorlevel 1 (
    echo [AuraClaw] Cannot enter the project directory: %~dp0
    goto :failed
)

set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [AuraClaw] Virtual environment Python was not found: %PYTHON_EXE%
    echo [AuraClaw] Create the virtual environment and install dependencies first.
    goto :failed
)

if not exist "%CD%\.env.dev" (
    echo [AuraClaw] Configuration file was not found: %CD%\.env.dev
    goto :failed
)

powershell.exe -NoLogo -NoProfile -Command "$ports=@(8000..8011)+@(8080); $used=(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue).LocalPort; $conflicts=[System.Collections.Generic.List[int]]::new(); foreach($port in $ports){if($used -contains $port){$conflicts.Add($port)}}; if($conflicts.Count -gt 0){Write-Host ('[AuraClaw] Required ports already in use: '+($conflicts -join ', ')); exit 1}"
if errorlevel 1 goto :ports_in_use

echo [AuraClaw] Checking database migration status...
"%PYTHON_EXE%" -m auraclaw migrate check --directory migrations
if errorlevel 1 goto :migration_failed

echo.
echo [AuraClaw] Starting the complete local topology...
echo [AuraClaw] Entry point: http://127.0.0.1:8080
echo [AuraClaw] Press Ctrl+C to stop all services.
echo.

"%PYTHON_EXE%" -m auraclaw serve --host 0.0.0.0
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [AuraClaw] Service exited with code: %EXIT_CODE%
    pause
)
exit /b %EXIT_CODE%

:ports_in_use
set "EXIT_CODE=2"
echo [AuraClaw] Another AuraClaw instance may already be running.
echo [AuraClaw] Stop the existing instance first, or continue using http://127.0.0.1:8080
pause
exit /b %EXIT_CODE%

:migration_failed
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [AuraClaw] Database migration check failed. Resolve the error above first.
pause
exit /b %EXIT_CODE%

:failed
set "EXIT_CODE=1"
echo.
pause
exit /b %EXIT_CODE%
