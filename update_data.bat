@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "C:\Python312\python.exe" goto python312
py -3 -I update_data.py
goto done
:python312
"C:\Python312\python.exe" -I update_data.py
:done
pause
