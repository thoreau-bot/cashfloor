@echo off
rem Update an existing install from a release zip.
rem   1. Close the serve window (the dashboard) first.
rem   2. Put the cashfloor-<version>.zip anywhere in this folder, or pass its path:
rem        scripts\update.bat "K:\somewhere\cashfloor-0.2.0.zip"
rem   3. Run this.  Your config.toml, data\, statements\, inbox\ and .venv are never touched.
setlocal
cd /d "%~dp0.."
set "ZIP=%~1"
if "%ZIP%"=="" for /f "delims=" %%f in ('dir /b /o-d cashfloor-*.zip 2^>nul') do if not defined ZIP set "ZIP=%%f"
if "%ZIP%"=="" (echo No cashfloor-*.zip found in %CD%. Drop the release zip here or pass its path. & pause & exit /b 1)
if not exist "%ZIP%" (echo %ZIP% not found. & pause & exit /b 1)
echo Updating from %ZIP% ...
set "TMPD=%TEMP%\cashfloor-update-%RANDOM%"
powershell -NoProfile -Command "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%TMPD%' -Force" || (echo Could not unpack the zip. & pause & exit /b 1)
set "SRC=%TMPD%"
if exist "%TMPD%\cashfloor\pyproject.toml" set "SRC=%TMPD%\cashfloor"
if not exist "%SRC%\pyproject.toml" (echo That zip does not look like a cashfloor release. & rd /s /q "%TMPD%" & pause & exit /b 1)
robocopy "%SRC%" "." /E /NFL /NDL /NJH /NJS /XD .venv data statements inbox browser-profiles .git /XF config.toml >nul
if errorlevel 8 (echo Copy failed. & rd /s /q "%TMPD%" & pause & exit /b 1)
rd /s /q "%TMPD%"
if exist ".venv\Scripts\activate.bat" (
  call .venv\Scripts\activate.bat
  pip install -q -e ".[dev,scrape]" || (echo Dependency install failed; run scripts\setup.bat. & pause & exit /b 1)
) else (
  echo No .venv yet - run scripts\setup.bat next.
)
for /f "tokens=3 delims== " %%v in ('findstr /b "version" pyproject.toml') do echo Installed %%v
echo Done. Start scripts\serve.bat; the version shows top-right in the dashboard.
pause
