#!/bin/sh
set -eu

export DISPLAY=:99
# docker commit 可能把运行中的 X socket 带进镜像；启动前只清理本显示器的陈旧标记。
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1440x900x24 -ac -nolisten tcp >/tmp/xvfb.log 2>&1 &
fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -listen 127.0.0.1 >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 6081 127.0.0.1:5900 >/tmp/novnc.log 2>&1 &

exec python refresher.py
