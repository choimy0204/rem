@echo off
cd /d "%~dp0"
python -c "import cv2, numpy, dxcam, pystray, mss, PIL" 2>nul
if errorlevel 1 (
  echo Installing required packages...
  python -m pip install -r requirements.txt
  if errorlevel 1 goto fail
)
python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo Installing PyInstaller...
  python -m pip install pyinstaller
  if errorlevel 1 goto fail
)
echo [1/2] Building VirtualMonitor.exe (app: virtual display + remote)...
python -m PyInstaller --noconfirm --onefile --noconsole --name VirtualMonitor --collect-all dxcam --collect-all comtypes --distpath dist --workpath build --specpath build app.py
if errorlevel 1 goto fail
call :selftest dist\VirtualMonitor.exe
if errorlevel 1 goto fail
echo [2/2] Building VirtualMonitorHost.exe (host: PC to be viewed)...
python -m PyInstaller --noconfirm --onefile --noconsole --name VirtualMonitorHost --collect-all dxcam --collect-all comtypes --hidden-import pystray._win32 --distpath dist --workpath build --specpath build host.py
if errorlevel 1 goto fail
call :selftest dist\VirtualMonitorHost.exe
if errorlevel 1 goto fail
echo.
echo Done: dist\VirtualMonitor.exe , dist\VirtualMonitorHost.exe
pause
exit /b 0
:selftest
start /wait "" "%~1" --selftest
if errorlevel 1 (
  echo.
  echo Selftest FAILED: %~1 cannot start. Close the error dialog and report its text.
  exit /b 1
)
exit /b 0
:fail
echo Build failed
pause
exit /b 1
