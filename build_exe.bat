@echo off
cd /d "%~dp0"
py -m pip install -r requirements-dev.txt
py build_exe.py
pause
