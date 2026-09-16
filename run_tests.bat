@echo off
cd /d "%~dp0"
py -m unittest selftest -v
pause
