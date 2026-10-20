@echo off
cd /d "%~dp0"
echo [1/2] Building VirtualMonitor.exe (app: virtual display + remote)...
python -m PyInstaller --noconfirm --onefile --noconsole --name VirtualMonitor --collect-all dxcam --collect-all comtypes --distpath dist --workpath build --specpath build app.py
if errorlevel 1 goto fail
echo [2/2] Building VirtualMonitorHost.exe (host: PC to be viewed)...
python -m PyInstaller --noconfirm --onefile --name VirtualMonitorHost --collect-all dxcam --collect-all comtypes --distpath dist --workpath build --specpath build host.py
if errorlevel 1 goto fail
echo.
echo Done: dist\VirtualMonitor.exe , dist\VirtualMonitorHost.exe
pause
exit /b 0
:fail
echo Build failed
pause
exit /b 1
