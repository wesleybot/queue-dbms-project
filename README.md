# queue-dbms-project

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg?style=flat&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Framework-Flask-green.svg?style=flat&logo=flask&logoColor=white)
![Redis](https://img.shields.io/badge/Database-Redis%20Stack-red.svg?style=flat&logo=redis&logoColor=white)
![Deployment](https://img.shields.io/badge/Deploy-Render%20%2B%20Cloudflare-orange.svg?style=flat&logo=render&logoColor=white)
![Status](https://img.shields.io/badge/Status-Active-success.svg)

> **🎓 113學年度第一學期 資料庫系統應用 期末專題報告**
>
> * **系級：** [你的系級]
> * **學號：** [你的學號]
> * **姓名：** [你的姓名]
> * **Live Demo:** [https://queue.xiandbms.ggff.net](https://queue.xiandbms.ggff.net)

---

## 📖 1. 緒論 (Introduction)

### 1.1 研究背景
在熱門店家或服務場所，傳統的實體排隊方式不僅造成現場擁擠，更讓顧客陷入漫長的「盲目等待」。實體叫號機硬體成本高昂且消耗紙張，不符合現代 ESG 環保趨勢。

### 1.2 研究動機
本專案提出 **「手機即叫號機 (BYOD)」** 的概念，利用 Redis 的高效能特性，打造一個 **跨平台、低延遲、高併發** 的雲端叫號系統。旨在解決顧客的「過號焦慮」，並透過即時推播讓顧客能靈活運用等待時間。

### 1.3 研究目的
1.  **即時性 (Real-time)：** 利用 Redis Pub/Sub 與 SSE 技術，達成 < 0.1 秒的狀態同步。
2.  **跨平台整合：** 同時支援 **Web (掃碼)** 與 **LINE 官方帳號** 操作。
3.  **數據決策：** 提供管理後台即時監控流量與「時段熱度分析」。

---

## 💻 2. 作品介紹 (Project Overview)

### 2.1 系統架構 (System Architecture)
本系統採用 **雲端原生 (Cloud Native)** 的分離式架構，確保高可用性與安全性。

* **前端：** HTML5, Bootstrap 5, JavaScript (EventSource/SSE)。
* **後端：** Python Flask, Gunicorn (Threaded Worker)。
* **資料庫：** Redis Cloud (Upstash/Redis Labs)。
* **部署設施：** Render (PaaS) + Cloudflare (WAF & CDN)。

### 2.2 操作流程 (Workflow)
1.  **取號：** 使用者掃描 QR Code 或在 LINE 點擊選單，系統生成唯一 Token 並回傳票號。
2.  **等待：** 手機畫面透過 SSE (Server-Sent Events) 即時跳動，顯示前方等待人數。
3.  **叫號：** 管理員在後台點擊「叫下一位」，系統自動廣播並更新狀態。
4.  **通知：** 使用者收到 LINE 推播與網頁震動提醒，前往櫃台。

### 2.3 作品亮點 (Highlights)
* **🔐 Token 雙重驗證機制：** 解決了 LINE 跳轉瀏覽器時 Session 遺失的問題，並防止惡意使用者透過修改網址 ID 偷看他人票券。
* **🤖 智慧自動結案：** 解決了「櫃台忘記按結束」導致系統卡死的問題。當呼叫下一位時，系統會自動搜尋並結案上一位 `Serving` 的顧客。
* **📡 廣播器架構 (Broadcaster Pattern)：** 為解決 Redis 免費版連線數限制，實作了全域廣播器，僅使用 **1 條** Redis 監聽連線即可服務大量前端使用者。

---

## ⚡ 3. 引用技術 (Technologies)

本專案深入應用了 **6 項** Redis 進階功能，解決了傳統 SQL 資料庫難以處理的痛點：

### Redis 進階功能 vs. 傳統 SQL

| Redis 功能 | 本專案應用場景 | 解決了什麼問題？ (vs 傳統 SQL) |
| :--- | :--- | :--- |
| **1. Streams** | 排隊佇列 (Queue) | **順序保證**：取代 SQL 複雜的鎖表機制，實現高效 FIFO 佇列，支援 Consumer Group。 |
| **2. Pub/Sub** | 即時廣播系統 | **低延遲通知**：實現 WebSocket 級別的即時推播，不需要前端頻繁輪詢 (Polling) 資料庫。 |
| **3. RediSearch** | 狀態查詢索引 | **高速計數**：使用 `FT.SEARCH` 瞬間計算「前方等待人數」，無需全表掃描 (`SELECT COUNT`)。 |
| **4. Aggregation** | 時段熱度分析 | **即時分析**：使用 `FT.AGGREGATE` 與 `APPLY` 運算，在資料庫層直接計算每小時來客量，無需 ETL。 |
| **5. Pipelines** | 交易原子性 | **降低延遲**：將寫入 Hash、Stream 與計數器的指令打包發送，大幅降低雲端網路來回時間 (RTT)。 |
| **6. Atomic Locks** | LINE 推播防重 | **併發控制**：利用 `SET NX EX` 實作分散式鎖，防止多個 Worker 同時發送重複的 LINE 訊息。 |

---

## 📊 4. 系統展示 (Screenshots)

### 使用者端 (RWD Design)
極簡設計，支援 QR Code 掃描與即時狀態更新。

| 取號首頁 | 排隊中 (即時跳號) | 叫號通知 (服務中) |
| :---: | :---: | :---: |
| ![Home](static/images/demo_mobile_1.png) | ![Waiting](static/images/demo_mobile_2.png) | ![Serving](static/images/demo_mobile_3.png) |

### 管理後台 (Dashboard)
結合 RediSearch 聚合查詢，即時顯示等待人數、服務狀態與時段熱度分析。

![Admin Dashboard](static/images/demo_admin_dashboard.png)

*(註：請將您的截圖放入 `static/images/` 資料夾)*

---

## 🛠️ 5. 安裝與執行 (Installation)

### 本地開發 (Local Development)

1.  **Clone 專案**
    ```bash
    git clone [https://github.com/](https://github.com/)[你的帳號]/queue-dbms-project.git
    cd queue-dbms-project
    ```

2.  **建立環境變數 (.env)**
    ```env
    LINE_CHANNEL_SECRET=你的LINE_Secret
    LINE_CHANNEL_ACCESS_TOKEN=你的LINE_Token
    # REDIS_URL=redis://... (若要連線雲端才填，本地留空)
    ```

3.  **安裝依賴套件**
    ```bash
    pip install -r requirements.txt
    ```

4.  **啟動服務**
    ```bash
    # 使用啟動腳本 (自動偵測環境與啟動 Gunicorn)
    ./start.sh
    ```

---

## 📝 6. 結論與心得 (Conclusion)

本專案成功展示了如何利用 **NoSQL (Redis)** 的特性來構建一個高併發、低延遲的現代化應用。透過 **Streams** 做佇列、**Pub/Sub** 做通知、**RediSearch** 做分析，我們證明了 Redis 不僅僅是一個快取工具，更是構建即時系統的核心引擎。

在開發過程中，我們克服了 **Redis 連線數限制**（透過廣播器模式解決）、**SSL 相容性問題** 以及 **Cloudflare WAF 誤擋** 等挑戰，最終交付了一個穩定且安全的雲端服務。

---

### 🔗 附件與連結

* **Live Demo:** [https://queue.xiandbms.ggff.net](https://queue.xiandbms.ggff.net)
* **GitHub:** [https://github.com/[你的帳號]/queue-dbms-project](https://github.com/[你的帳號]/queue-dbms-project)

---
© 2024 Queue Ticket System Project. All Rights Reserved.