@echo off
rem Game update (publisher): rebuild the official DB and translation pack, then upload. See tools\game_update.py
chcp 65001 >nul
cd /d "%~dp0"
if exist "C:\Python312\python.exe" goto python312
py -3 -I tools\game_update.py %*
goto done
:python312
"C:\Python312\python.exe" -I tools\game_update.py %*
:done
pause
