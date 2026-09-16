@echo off
cd /d "%~dp0"
echo Tryb symulatora - bez PM125.
echo Scenariusze: pass ^| voltage_fail ^| current_fail ^| read_error ^| load_error ^| disconnected ^| no_dut
set MODE=%1
if "%MODE%"=="" set MODE=pass
py app.py --simulate --simulate-mode %MODE%
if errorlevel 1 pause
