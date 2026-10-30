@echo off
setlocal
cd /d "%~dp0"

echo Installing requirements...
python -m pip install -r requirements.txt
if errorlevel 1 goto failed

echo Installing PyInstaller...
python -m pip install pyinstaller
if errorlevel 1 goto failed

echo Building Roblox Auto-Rejoiner Executable...
echo This will take a moment...
python -m PyInstaller --noconfirm --clean --distpath "app" --workpath "build" "main_gui.spec"
if errorlevel 1 goto failed

echo Build complete! The executable is app\Roblox Auto-Rejoiner Studio (Icon).exe.
exit /b 0

:failed
echo.
echo Setup or build failed. Check the error above and try again.
pause
exit /b 1
