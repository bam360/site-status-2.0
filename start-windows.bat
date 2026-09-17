@echo off
rem One-click setup + launcher for Windows.
rem Installs Python if missing, installs dependencies, creates config.yaml
rem on first run, then starts the monitor and opens the dashboard.
setlocal
cd /d "%~dp0"
title site-status

set PY=
py -3 --version >nul 2>nul
if not errorlevel 1 set PY=py -3
if not defined PY (
    python --version >nul 2>nul
    if not errorlevel 1 set PY=python
)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)

if not defined PY (
    echo Python is not installed yet - downloading the official installer...
    curl -L -o "%TEMP%\python-installer.exe" https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
    if errorlevel 1 goto :fail_download
    echo Installing Python - this takes a minute, a progress window will appear...
    start /wait "" "%TEMP%\python-installer.exe" /passive InstallAllUsers=0 PrependPath=1 Include_test=0
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    ) else (
        goto :fail_install
    )
)

echo Installing dependencies...
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :fail_pip

if not exist config.yaml (
    copy config.example.yaml config.yaml >nul
    echo.
    echo FIRST RUN: Notepad will now open the configuration file.
    echo List the devices you want to monitor, then save and close Notepad.
    echo.
    pause
    notepad config.yaml
)

echo.
echo Starting site-status. Keep this window open - closing it stops monitoring.
echo Dashboard: http://localhost:8080
start "" http://localhost:8080
%PY% -m sitestatus --config config.yaml
echo.
echo site-status stopped.
pause
goto :eof

:fail_download
echo.
echo Could not download Python. Check your internet connection, or install
echo Python yourself from https://www.python.org/downloads/ and run this again.
pause
goto :eof

:fail_install
echo.
echo Python installation did not finish. Install it from
echo https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo then run this file again.
pause
goto :eof

:fail_pip
echo.
echo Failed to install dependencies. Check your internet connection and
echo run this file again.
pause
goto :eof
