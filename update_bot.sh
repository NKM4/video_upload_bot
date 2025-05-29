#!/usr/bin/env bash
set -e
cd /home/nkm2/Desktop/BOT/video_upload_bot
echo "[BOT更新] $(date)"
git pull --ff-only origin main      # ← Git を使っていなければ省いてOK
sudo systemctl restart video_upload_bot
echo "[BOT再起動完了]"
