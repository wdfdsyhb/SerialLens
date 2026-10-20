@echo off
rem SerialLens Web agent launcher (port 8300)
rem
rem Access password source, in priority order:
rem   1. env var SERIALLENS_ACCESS_PASSWORD
rem   2. webapp\.env.local (local password file, not in git)
rem   3. auto-generated, printed once at startup
rem
rem Bind host: defaults to 127.0.0.1 (this machine only).
rem For a LAN demo only:  set SERIALLENS_BIND_HOST=0.0.0.0
rem Make sure the password is strong before doing that.
rem
rem Started automatically from the Startup folder shortcut, so this must not
rem pause unconditionally -- otherwise a console window waits for a keypress
rem after every login. Pass --no-pause to skip the final pause.
cd /d %~dp0
set PYTHONUTF8=1
if "%SERIALLENS_BIND_HOST%"=="" set SERIALLENS_BIND_HOST=127.0.0.1
echo [SerialLens] bind: %SERIALLENS_BIND_HOST%:8300
python -m uvicorn webapp.app:app --host %SERIALLENS_BIND_HOST% --port 8300
if "%~1"=="--no-pause" exit /b
pause
