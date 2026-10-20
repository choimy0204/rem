@echo off
cd /d "%~dp0"
echo Installing required packages...
python -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo.
echo Done. You can now run the host and the app.
pause
exit /b 0
:fail
echo.
echo Install failed. Check that Python is installed and added to PATH.
pause
exit /b 1
