@echo off
rem Run from any working directory, with no installation or network access.
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -X utf8 -m context_hub %*
) else (
  python -X utf8 -m context_hub %*
)
