@echo off
rem One-time setup on Windows: create the virtualenv, install cashfloor and
rem the Playwright browser, run the tests, write config.toml.
cd /d "%~dp0.."
where python >nul 2>nul || (echo Python 3.11+ not found on PATH. Install from python.org with "Add to PATH" ticked. & pause & exit /b 1)
if not exist ".venv" python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -e ".[dev,scrape]" || (pause & exit /b 1)
playwright install chromium
pytest || (pause & exit /b 1)
if not exist "config.toml" cashfloor init
echo.
echo Setup complete. Next: edit config.toml, then  cashfloor simplefin setup ^<TOKEN^>
echo Use scripts\shell.bat to open a prompt with the environment active.
pause
