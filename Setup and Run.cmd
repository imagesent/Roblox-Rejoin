@echo off
setlocal
cd /d "%~dp0"

set "APP=%~dp0app\Roblox Auto-Rejoiner Studio (Icon).exe"
if exist "%APP%" goto run

where python >nul 2>nul
if errorlevel 1 goto python_missing

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto python_too_old

echo The packaged app is missing. Installing dependencies and building it now...
call "%~dp0build.cmd"
if errorlevel 1 exit /b 1
if not exist "%APP%" goto build_missing

:run
echo Launching Roblox Auto-Rejoiner...
start "" "%APP%"
exit /b 0

:python_missing
echo Python 3.10 or newer is required to build the app.
echo Install Python from https://www.python.org/downloads/ and try again.
pause
exit /b 1

:python_too_old
echo Python 3.10 or newer is required to build the app.
pause
exit /b 1

:build_missing
echo The build finished, but the application executable was not created.
pause
exit /b 1
