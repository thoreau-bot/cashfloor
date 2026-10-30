@echo off
rem Open a Command Prompt in the repo with the virtualenv activated.
rem Double-click this file, or pin a shortcut to it.  Run scripts\setup.bat first.
cd /d "%~dp0.."
if not exist ".venv\Scripts\activate.bat" (
    echo No virtualenv found. Run scripts\setup.bat first.
    pause
    exit /b 1
)
cmd /k .venv\Scripts\activate.bat
