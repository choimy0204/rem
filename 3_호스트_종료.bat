@echo off
echo Stopping VirtualMonitor host...
taskkill /f /t /im VirtualMonitorHost.exe >nul 2>&1 && echo - stopped VirtualMonitorHost.exe
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match 'host\.py' }; $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('- stopped python host.py (PID ' + $_.ProcessId + ')') }"
echo Done. (If a host was started as administrator, run this file as administrator too.)
pause
