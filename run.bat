@echo off
setlocal
set "ROOT=%~dp0"
set "VENV_PY=%ROOT%venv\Scripts\python.exe"

if not exist "%VENV_PY%" goto bootstrap
"%VENV_PY%" -m pip --version >nul 2>&1
if errorlevel 1 goto bootstrap
goto run

:bootstrap
echo [run.bat] Virtual environment missing or unhealthy. Bootstrapping...
py -3.12 "%ROOT%bootstrap_env.py" --venv-dir venv --fresh >nul 2>&1
if errorlevel 1 (
	python "%ROOT%bootstrap_env.py" --venv-dir venv --fresh
	if errorlevel 1 (
		echo [run.bat] Bootstrap failed.
		exit /b 1
	)
)

:run
"%VENV_PY%" "%ROOT%visa_appointment_checker.py" %*
pause