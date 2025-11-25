    #!/bin/bash

    GREEN='\033[0;32m'
    BLUE='\033[0;34m'
    RED='\033[0;31m'
    NC='\033[0m'

    echo -e "${BLUE}=========================================${NC}"
    echo -e "${BLUE}   🚀 Queue Ticket System 啟動中...   ${NC}"
    echo -e "${BLUE}=========================================${NC}"

    # 1. [關鍵修正] 強制載入 .env 檔案變數
    if [ -f .env ]; then
        echo -e "${GREEN}[設定] 偵測到 .env 檔案，正在匯入環境變數...${NC}"
        # 使用 export 將 .env 裡的變數匯入當前 Shell
        export $(grep -v '^#' .env | xargs)
    else
        echo -e "${RED}[警告] 找不到 .env 檔案！LINE 推播可能會失敗！${NC}"
    fi

    # 2. 啟動 Gunicorn
    echo -e "${GREEN}[1/2] 正在啟動 Python Gunicorn Server (Threaded Mode)...${NC}"

    # 檢查是否安裝 gunicorn
    if ! pip show gunicorn > /dev/null 2>&1; then
        echo -e "${RED}[錯誤] 尚未安裝 gunicorn，請執行: pip install gunicorn python-dotenv line-bot-sdk${NC}"
        exit 1
    fi

# 改用 sync 模式，並開 5 個 Workers (代表同時能有 5 個人連線看 SSE，Demo 夠用了)
nohup gunicorn -k sync -w 3 -b 127.0.0.1:5000 app:app > server.log 2>&1 &
    SERVER_PID=$!

    sleep 3

    if ! ps -p $SERVER_PID > /dev/null; then
        echo -e "${RED}[嚴重錯誤] Gunicorn 啟動失敗！${NC}"
        echo -e "${RED}以下是 server.log 的錯誤內容：${NC}"
        echo "----------------------------------------"
        cat server.log
        echo "----------------------------------------"
        exit 1
    else
        echo -e "${GREEN}Gunicorn 成功運行中 (PID: $SERVER_PID)${NC}"
    fi

    # 3. 啟動 Cloudflared
    echo -e "${GREEN}[2/2] 正在啟動 Cloudflare Tunnel...${NC}"
    echo -e "你的網址: https://queue.xiandbms.ggff.net/"

    # 啟動 Tunnel
    cloudflared tunnel run redis-queue-qr

    # 結束時清理
    kill $SERVER_PID
    echo -e "${BLUE}系統已關閉。${NC}"