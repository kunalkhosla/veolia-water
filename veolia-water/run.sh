#!/bin/sh
# Start a virtual X display (Chromium must run headed to clear Cloudflare
# Turnstile) without xvfb-run, which would require xauth.
Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 1
exec python3 veolia_water.py
