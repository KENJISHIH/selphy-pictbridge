#!/bin/zsh
# 雙擊啟動 SELPHY 編輯器，並開瀏覽器（用 Safari 才能直接開 iPhone 的 HEIC）
# Double-click to start the editor and open it in Safari.
cd "$(dirname "$0")"
# 本機設定（不進 git）：可在 selphy.local.conf 寫 export SELPHY_URI=ipp://<印表機IP>:631/ipp/print
# Optional local config (git-ignored). Without it the server searches the network for a SELPHY.
[ -f selphy.local.conf ] && source selphy.local.conf
( sleep 2; /usr/bin/open -a Safari http://127.0.0.1:8765 ) &
PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11
[ -x "$PY" ] || PY=python3
exec "$PY" server.py
