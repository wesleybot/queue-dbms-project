# queue-dbms-project

🚀 Redis 驅動的雲端智慧叫號系統 (Cloud-Native Queue System)🎓 113學年度第一學期 資料庫系統應用 期末專題報告系級： [你的系級]學號： [你的學號]姓名： [你的姓名]Live Demo: https://queue.xiandbms.ggff.net📖 1. 緒論 (Introduction)1.1 研究背景與動機在熱門店家或服務場所，傳統的實體排隊方式不僅造成現場擁擠，更讓顧客陷入漫長的「盲目等待」。實體叫號機硬體成本高昂且消耗紙張，不符合現代 ESG 環保趨勢。本專案提出 「手機即叫號機 (BYOD)」 的概念，利用 Redis 的高效能特性，打造一個 跨平台、低延遲、高併發 的雲端叫號系統。1.2 預期目標即時性 (Real-time)： 利用 Redis Pub/Sub 與 SSE 技術，達成 < 0.1 秒的狀態同步。跨平台整合： 同時支援 Web (掃碼) 與 LINE 官方帳號 操作。數據決策： 提供管理後台即時監控流量與「時段熱度分析」。🏗️ 2. 系統架構與設計 (System Architecture)本系統採用 雲端原生 (Cloud Native) 的分離式架構，確保高可用性與安全性。2.1 系統部署圖 (Deployment Diagram)程式碼片段graph TB
    subgraph "Client Side (客戶端)"
        Browser[手機/電腦瀏覽器]
        LineApp[LINE App]
    end

    subgraph "Security Layer (資安防護)"
        CF[Cloudflare Proxy (WAF & DDoS 防護)]
    end

    subgraph "Render Cloud (應用程式層)"
        Gunicorn[Gunicorn Server (Threaded)]
        Flask[Flask App]
        Worker[Background Listener Thread]
    end

    subgraph "Redis Cloud (資料庫層)"
        RedisDB[(Redis Stack)]
        
        subgraph "Redis Features"
            Stream[Stream (Queue)]
            Index[RediSearch Index]
            PubSub[Pub/Sub Channel]
            Hash[Hash Data]
        end
    end

    Browser -- "HTTPS / SSE" --> CF
    LineApp -- "Webhook" --> CF
    CF -- "SSL Tunnel" --> Gunicorn
    Gunicorn --> Flask
    
    Flask -- "Read/Write" --> RedisDB
    Worker -- "Subscribe/Listen" --> RedisDB
    
    RedisDB --- Stream
    RedisDB --- Hash
    RedisDB --- Index
2.2 核心流程：叫號與推播 (Sequence Diagram)展示系統如何利用 Redis Stream 確保順序，並透過 Pub/Sub 達成多端同步通知。程式碼片段sequenceDiagram
    autonumber
    participant Admin as 管理員 (Web)
    participant Server as Flask Server
    participant Redis as Redis DB
    participant Worker as Background Worker
    participant User as 使用者 (Web/LINE)

    Note over Admin, Server: 動作：叫下一號

    Admin->>Server: POST /counter/next
    
    Server->>Redis: FT.SEARCH (找上一位 Serving 的票)
    Redis-->>Server: 返回舊票 ID
    Server->>Redis: HSET status='done' (自動結案)
    
    Server->>Redis: XREADGROUP (從 Stream 讀取下一位)
    Redis-->>Server: 返回新票 ID
    
    Server->>Redis: HSET status='serving' (更新新票狀態)
    Server->>Redis: PUBLISH channel (廣播叫號訊息)
    
    Server-->>Admin: 回傳 JSON (更新後台畫面)

    par 平行處理通知 (非同步)
        Redis->>Worker: 收到 Pub/Sub 訊息
        Worker->>Worker: 檢查 Redis 鎖 (Deduplication)
        
        Worker->>User: SSE 推播 (網頁數字跳動)
        
        alt 是 LINE 使用者
            Worker->>User: LINE Messaging API (發送訊息)
        end
    end
⚡ 3. 引用技術與 Redis 進階應用本專案深入應用了 6 項 Redis 進階功能，解決了傳統 SQL 資料庫難以處理的痛點：Redis 功能本專案應用場景解決了什麼問題？ (vs. SQL)1. Streams排隊佇列 (queue_stream)順序保證：取代 SQL 複雜的鎖表機制，實現高效 FIFO 佇列，支援 Consumer Group。2. Pub/Sub即時廣播系統低延遲通知：實現 WebSocket 級別的即時推播，不需要前端頻繁輪詢 (Polling) 資料庫。3. RediSearch狀態查詢索引 (idx:ticket)高速計數：使用 FT.SEARCH 瞬間計算「前方等待人數」，無需全表掃描 (SELECT COUNT)。4. Aggregation時段熱度分析即時分析：使用 FT.AGGREGATE 與 APPLY 運算，在資料庫層直接計算每小時來客量，無需 ETL。5. Pipelines交易原子性降低延遲：將寫入 Hash、Stream 與計數器的指令打包發送，大幅降低雲端網路來回時間 (RTT)。6. Atomic LocksLINE 推播防重併發控制：利用 SET NX EX 實作分散式鎖，防止多個 Worker 同時發送重複的 LINE 訊息。📸 4. 作品展示 (Screenshots)📱 使用者端 (RWD Design)極簡設計，支援 QR Code 掃描與即時狀態更新。取號首頁排隊中 (即時跳號)叫號通知 (服務中)🖥️ 管理後台 (Dashboard)即時監控： 結合 RediSearch 聚合查詢，即時顯示等待人數與服務狀態。數據分析： 自動計算「今日平均服務時間」與「時段熱度」。(註：請將您的截圖放入 static/images/ 資料夾)🛠️ 5. 安裝與執行 (Installation)本地開發 (Local Development)Clone 專案Bashgit clone https://github.com/[你的帳號]/queue-dbms-project.git
cd queue-dbms-project
建立環境變數 (.env)程式碼片段LINE_CHANNEL_SECRET=你的LINE_Secret
LINE_CHANNEL_ACCESS_TOKEN=你的LINE_Token
# REDIS_URL=redis://... (若要連線雲端才填，本地留空)
安裝依賴套件Bashpip install -r requirements.txt
啟動服務Bash# 使用啟動腳本 (自動偵測環境與啟動 Gunicorn)
./start.sh
📝 6. 結論與心得 (Conclusion)本專案成功展示了如何利用 NoSQL (Redis) 的特性來構建一個高併發、低延遲的現代化應用。透過 Streams 做佇列、Pub/Sub 做通知、RediSearch 做分析，我們證明了 Redis 不僅僅是一個快取工具，更是構建即時系統的核心引擎。在開發過程中，我們克服了 Redis 連線數限制（透過廣播器模式解決）、SSL 相容性問題 以及 Cloudflare WAF 誤擋 等挑戰，最終交付了一個穩定且安全的雲端服務。© 2024 Queue Ticket System Project. All Rights Reserved.