@echo off
REM ===================================================================
REM  VYOM - Agentic GIS  :  one-click API server launcher
REM  Just double-click this file. It checks everything for you and
REM  installs anything that is missing before starting the server.
REM ===================================================================
title VYOM API Server
setlocal

REM -- Move to the folder this .bat lives in (project root) -----------
cd /d "%~dp0"

echo ===================================================================
echo   VYOM - Agentic GIS for Indian Disaster Analysis
echo ===================================================================
echo.

set "PYEXE=%~dp0myenv\Scripts\python.exe"
set "PYTHONPATH=%~dp0src"

REM ===================================================================
REM  STEP 1 - make sure the Python environment exists
REM ===================================================================
if not exist "%PYEXE%" (
    echo [SETUP] Python environment "myenv" not found - creating it now...
    echo         This happens only once and may take a minute.
    echo.
    REM Try the Windows "py" launcher first, then a plain "python".
    py -3 -m venv "%~dp0myenv"  2>nul
    if not exist "%PYEXE%"  python -m venv "%~dp0myenv"  2>nul
    if not exist "%PYEXE%" (
        echo [ERROR] Could not create the Python environment automatically.
        echo         Python 3.11 must be installed on this PC.
        echo         Download it from https://www.python.org/downloads/
        echo.
        pause
        exit /b 1
    )
    echo [SETUP] Environment created.
    echo.
)

REM ===================================================================
REM  STEP 2 - check that all server dependencies are installed
REM           (this single import loads the entire dependency chain)
REM ===================================================================
echo [CHECK] Verifying required packages...
"%PYEXE%" -c "import vyom.api.app" 1>nul 2>nul
if not errorlevel 1 goto DEPS_OK

echo [CHECK] Some packages are missing or incomplete.
echo [SETUP] Installing dependencies from requirements.txt...
echo         (first run only - please wait, this can take a few minutes)
echo.
"%PYEXE%" -m pip install --upgrade pip
"%PYEXE%" -m pip install -r "%~dp0requirements.txt"
echo.

REM -- Re-check after installing --------------------------------------
echo [CHECK] Re-verifying packages...
"%PYEXE%" -c "import vyom.api.app" 1>nul 2>nul
if not errorlevel 1 goto DEPS_OK

REM -- Still failing: show the real error so it can be diagnosed ------
echo.
echo ===================================================================
echo  [ERROR] The server still cannot start. Details below:
echo ===================================================================
"%PYEXE%" -c "import vyom.api.app"
echo.
echo  Common causes:
echo    - No internet / proxy blocked the package download
echo    - A package failed to build (e.g. rasterio, psycopg2)
echo  Send the red text above to the developer.
echo ===================================================================
pause
exit /b 1

:DEPS_OK
echo [CHECK] All packages present.
echo.

REM ===================================================================
REM  STEP 3 - friendly info, open browser, then launch the server
REM ===================================================================
echo ===================================================================
echo   Server URL ............  http://127.0.0.1:8000
echo   Testing UI (Swagger) ..  http://127.0.0.1:8000/docs
echo.
echo   A browser will open automatically in a few seconds.
echo   To STOP the server: close this window or press Ctrl+C.
echo ===================================================================
echo.

REM -- Open the Swagger UI shortly after the server boots -------------
start "" /min cmd /c "timeout /t 6 >nul & start http://127.0.0.1:8000/docs"

REM -- Launch the server (blocks until the window is closed) ----------
"%PYEXE%" -m vyom.api

REM -- If the server exits/crashes, keep the window open --------------
echo.
echo ===================================================================
echo  The VYOM server has stopped.
echo ===================================================================
pause
endlocal
