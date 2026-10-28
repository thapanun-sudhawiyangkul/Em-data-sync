@echo off
rem One-click sync. Extra options are passed on, e.g.  run_sync.bat --dry-run
cd /d "%~dp0"
py -3 "%~dp0sync_passport.py" %*
if errorlevel 1 echo Sync finished with errors - see logs\sync_passport.log
if "%1"=="" pause
