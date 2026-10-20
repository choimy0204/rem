@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] VirtualMonitor.exe (앱: 가상 디스플레이 + 원격) 빌드...
python -m PyInstaller --noconfirm --onefile --noconsole --name VirtualMonitor ^
  --collect-all dxcam --collect-all comtypes --distpath dist --workpath build --specpath build app.py
if errorlevel 1 goto fail
echo [2/2] VirtualMonitorHost.exe (호스트: 보여줄 PC) 빌드...
python -m PyInstaller --noconfirm --onefile --name VirtualMonitorHost ^
  --collect-all dxcam --collect-all comtypes --distpath dist --workpath build --specpath build host.py
if errorlevel 1 goto fail
echo.
echo 완료: dist\VirtualMonitor.exe , dist\VirtualMonitorHost.exe
pause
exit /b 0
:fail
echo 빌드 실패
pause
exit /b 1
