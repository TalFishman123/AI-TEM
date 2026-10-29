@echo off
rem Starts the UTEM state logger with the PyJEM Python. Used by the scheduled task (install_task.ps1);
rem double-click to run it in a console instead. Edit PYTHON if the PyJEM environment is elsewhere.
set PYTHON=%USERPROFILE%\.conda\envs\vjem38\pythonw.exe
cd /d "%~dp0.."
"%PYTHON%" -m utem_logger --config "%~dp0..\config.json"
