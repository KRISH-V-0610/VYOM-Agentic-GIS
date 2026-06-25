@echo off
REM ===================================================================
REM  VYOM - Agentic GIS  :  setup pre-flight check
REM  Double-click to verify this PC is ready to run VYOM:
REM    Python, packages, .env keys, database, and config registries.
REM  Run this BEFORE Start-VYOM-Server.bat.
REM ===================================================================
title VYOM Setup Check
setlocal

cd /d "%~dp0"

set "PYEXE=%~dp0myenv\Scripts\python.exe"
set "PYTHONPATH=%~dp0src"
set "PYTHONIOENCODING=utf-8"

REM -- If the environment is missing, point the user to the launcher --
if not exist "%PYEXE%" (
    echo [ERROR] Python environment "myenv" was not found.
    echo.
    echo         Run  Start-VYOM-Server.bat  first - it creates the
    echo         environment and installs the required packages.
    echo.
    pause
    exit /b 1
)

REM -- Run the Python checker (it prints the green/red checklist) -----
"%PYEXE%" "%~dp0scripts\check_setup.py"
set "RC=%ERRORLEVEL%"

if "%RC%"=="0" goto ALL_GOOD

REM -- Something failed. Offer to auto-install missing packages -------
echo.
echo -------------------------------------------------------------------
echo  Some checks failed above.
echo  If any are MISSING PACKAGES, they can be downloaded now.
echo  (This will NOT fix database or .env problems - those are manual.)
echo -------------------------------------------------------------------
set /p DOINSTALL="Download and install required packages now? (Y/N): "
if /i not "%DOINSTALL%"=="Y" goto SKIP_INSTALL

echo.
echo [SETUP] Installing dependencies from requirements.txt...
echo         (needs internet - this can take a few minutes)
echo.
"%PYEXE%" -m pip install --upgrade pip
"%PYEXE%" -m pip install -r "%~dp0requirements.txt"

echo.
echo [CHECK] Re-running the setup check...
echo.
"%PYEXE%" "%~dp0scripts\check_setup.py"
set "RC=%ERRORLEVEL%"
goto REPORT

:SKIP_INSTALL
echo.
echo Skipped package installation.
goto REPORT

:ALL_GOOD
:REPORT
echo.
if "%RC%"=="0" (
    echo You can now double-click  Start-VYOM-Server.bat  to launch VYOM.
) else (
    echo Some items still need attention - see the [FAIL] lines above.
    echo Database / .env problems must be fixed manually, then run this again.
)
echo.
pause
endlocal
