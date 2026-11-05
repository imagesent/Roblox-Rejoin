@echo off
setlocal
cd /d "%~dp0"

set "APP=%~dp0app\Roblox Rejoiner.exe"
if not exist "%APP%" goto build

powershell -NoProfile -ExecutionPolicy Bypass -Command "$exe=Get-Item -LiteralPath '%APP%'; $inputs=@((Get-ChildItem -LiteralPath '%~dp0src' -Recurse -File),(Get-ChildItem -LiteralPath '%~dp0assets' -Recurse -File),(Get-Item -LiteralPath '%~dp0roblox_rejoiner.spec'),(Get-Item -LiteralPath '%~dp0README.md'),(Get-Item -LiteralPath '%~dp0requirements.txt')); if ($inputs | Where-Object { $_.LastWriteTime -gt $exe.LastWriteTime }) { exit 1 }"
if not errorlevel 1 goto run

:build

where python >nul 2>nul
if errorlevel 1 goto python_missing

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto python_too_old

echo Installing app dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 goto failed

echo Installing PyInstaller...
python -m pip install pyinstaller
if errorlevel 1 goto failed

echo Building Roblox Rejoiner...
python -m PyInstaller --noconfirm --clean --distpath "app" --workpath "build" "roblox_rejoiner.spec"
if errorlevel 1 goto failed
if not exist "%APP%" goto build_missing

:run
echo Launching Roblox Rejoiner...
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

:failed
echo.
echo Setup or build failed. Check the error above and try again.
pause
exit /b 1

:build_missing
echo The build finished, but the application executable was not created.
pause
exit /b 1
