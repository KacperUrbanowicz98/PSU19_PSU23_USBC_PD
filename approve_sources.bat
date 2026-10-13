@echo off
REM Zatwierdza aktualne zrodla: zapisuje ich sumy SHA-256 do approved_sources.json.
REM Uruchom PO sprawdzeniu zmian, przed budowaniem wydania.
cd /d "%~dp0"
py build_exe.py --approve
pause
