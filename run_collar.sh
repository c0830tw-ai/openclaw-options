#!/bin/bash
# cron wrapper：載入 .env 後執行主腳本

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 載入 .env
if [ -f "$DIR/.env" ]; then
    export $(grep -v '^#' "$DIR/.env" | xargs)
fi

# 建立 logs 目錄
mkdir -p "$DIR/logs"

/usr/bin/python3 "$DIR/shioaji_collar.py" >> "$DIR/logs/collar.log" 2>&1
