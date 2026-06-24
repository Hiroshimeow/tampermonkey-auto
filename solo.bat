@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_CMD=python"
where python >nul 2>nul
if errorlevel 1 set "PYTHON_CMD=py"

%PYTHON_CMD% "%~dp0solo.py" %*
exit /b %ERRORLEVEL%
