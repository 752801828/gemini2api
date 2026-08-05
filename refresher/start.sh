#!/bin/sh
set -eu

export DISPLAY=:99
Xvfb :99 -screen 0 1440x900x24 -ac -nolisten tcp >/tmp/xvfb.log 2>&1 &
fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -listen 127.0.0.1 >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 6081 127.0.0.1:5900 >/tmp/novnc.log 2>&1 &

exec python refresher.py
