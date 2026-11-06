@echo off
cd /d "%~dp0"
if exist "C:\Python312\pythonw.exe" goto python312
start "" pyw -3 main.py
goto done
:python312
start "" "C:\Python312\pythonw.exe" main.py
:done
