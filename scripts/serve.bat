@echo off
rem Start the cashfloor dashboard and open it in your browser.  Close this window to stop.
cd /d "%~dp0.."
if not exist ".venv\Scripts\cashfloor.exe" (echo Run scripts\setup.bat first. & pause & exit /b 1)
.venv\Scripts\cashfloor.exe serve --open
pause
