#!/bin/sh
# Startup script: read TELETHON_PROXY from proxy.txt if present, then run gateway
if [ -f /opt/hermes/proxy.txt ]; then
    _PROXY=$(cat /opt/hermes/proxy.txt | tr -d '[:space:]')
    if [ -n "$_PROXY" ]; then
        export TELETHON_PROXY="$_PROXY"
        echo "[startup] Loaded TELETHON_PROXY from proxy.txt"
    fi
fi

exec hermes gateway run
