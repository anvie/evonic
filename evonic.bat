@echo off
setlocal
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "EVONIC_HOME=%~dp0"
if "%EVONIC_HOME:~-1%"=="\" set "EVONIC_HOME=%EVONIC_HOME:~0,-1%"
if exist "%EVONIC_HOME%\venv\Scripts\python.exe" (
    "%EVONIC_HOME%\venv\Scripts\python.exe" "%EVONIC_HOME%\cli\__main__.py" %*
) else (
    python "%EVONIC_HOME%\cli\__main__.py" %*
)
endlocal
