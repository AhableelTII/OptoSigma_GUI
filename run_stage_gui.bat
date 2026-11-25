@echo off
rem Starts the stage GUI. The stage RE-ZEROES (moves to its home switch and back) as soon as it connects.
cd /d "%~dp0"
python stage_gui.py %*
if errorlevel 1 pause
