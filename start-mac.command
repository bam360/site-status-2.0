#!/bin/bash
# One-click setup + launcher for macOS.
# Sets up a virtualenv, installs dependencies, creates config.yaml on
# first run, then starts the monitor and opens the dashboard.
set -u
cd "$(dirname "$0")"

echo "== site-status =="

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required. Install it from https://www.python.org/downloads/"
    echo "then run this file again."
    read -r -p "Press Enter to close..."
    exit 1
fi

# On a fresh Mac, the first python3 run may pop Apple's developer-tools
# install dialog. Click Install, let it finish, then run this again.
if ! python3 -c 'import sys' >/dev/null 2>&1; then
    echo "macOS is asking to install its command line tools first."
    echo "Click Install in the dialog, wait for it to finish, then run this file again."
    read -r -p "Press Enter to close..."
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "Setting up (first run only)..."
    python3 -m venv .venv || { echo "Could not create virtualenv."; read -r -p "Press Enter to close..."; exit 1; }
fi

echo "Installing dependencies..."
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt || {
    echo "Failed to install dependencies - check your internet connection and try again."
    read -r -p "Press Enter to close..."
    exit 1
}

if [ ! -f config.yaml ]; then
    cp config.example.yaml config.yaml
    echo
    echo "FIRST RUN: config.yaml will now open in TextEdit."
    echo "List the devices you want to monitor, then save (Cmd-S) and close it."
    open -t config.yaml
    read -r -p "Press Enter once you have saved and closed config.yaml..."
fi

echo
echo "Starting site-status. Keep this window open - closing it stops monitoring."
echo "Dashboard: http://localhost:8080"
( sleep 2; open "http://localhost:8080" ) &
exec .venv/bin/python -m sitestatus --config config.yaml
