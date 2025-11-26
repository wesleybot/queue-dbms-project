# queue_core.py
import time
import json
import os
import redis
from datetime import datetime

# ---------------------------------------------------------
# [連線設定] 智慧連線與連線池限制 (max_connections=10)
# ---------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL")

if REDIS_URL:
    # 雲端模式：使用 ConnectionPool
    pool = redis.ConnectionPool.from_url(
        REDIS_URL, 
        decode_responses=True, 
        max_connections=10, 
        socket_timeout=5
    )
    r = redis.Redis(connection_pool=pool)
else:
    # 本地模式
    r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# 確保 Index 存在
try:
    r.execute_command(
        "FT.CREATE", "idx:ticket", "ON", "HASH", "PREFIX", "1", "ticket:",
        "SCHEMA", "service", "TEXT", "status", "TAG", "created_at", "NUMERIC", "SORTABLE"
    )
except Exception as e:
    print(f"Index creation skipped or failed: {e}")

# ---------------------------------------------------------
# create_ticket: 創建票券並寫入 Stream
# ---------------------------------------------------------
def create_ticket(service: str, line_user_id: str = "") -> dict:
    pipe = r.pipeline()
    ticket_id = r.incr("ticket:global:id")
    
    number = ticket_id
    now = int(time.time())
    ticket_key = f"ticket:{ticket_id}"
    stream_key = f"queue_stream:{service}"

    mapping_data = {
        "number": number,
        "service": service,
        "status": "waiting",
        "created_at": now,
        "called_at": "",
        "counter": "",
        "line_user_id": line_user_id 
    }
    
    pipe.hset(ticket_key, mapping=mapping_data)
    pipe.xadd(stream_key, {"ticket_id": ticket_id}, maxlen=1000)
    pipe.execute()

    return {
        "ticket_id": ticket_id,
        "number": number,
        "service": service,
        "created_at": now
    }

# ---------------------------------------------------------
# call_next: 叫號，讀取 Stream，並發出 Pub/Sub
# ---------------------------------------------------------
def call_next(service: str, counter_name: str) -> dict | None:
    stream_key = f"queue_stream:{service}"
    group_name = "counters_group"
    consumer_name = counter_name

    try:
        r.xgroup_create(stream_key, group_name, id="0", mkstream=True)
    except redis.exceptions.ResponseError:
        pass

    messages = r.xreadgroup(group_name, consumer_name, {stream_key: ">"}, count=1)

    if not messages:
        return None
    
    stream_data = messages[0][1]  
    if not stream_data:
        return None

    message_id, data = stream_data[0]
    ticket_id = data["ticket_id"]

    r.xack(stream_key, group_name, message_id)

    now = int(time.time())
    ticket_key = f"ticket:{ticket_id}"

    if not r.exists(ticket_key):
        return None

    created_at = int(r.hget(ticket_key, "created_at") or now)

    # 更新狀態為 serving
    r.hset(ticket_key, mapping={
        "status": "serving",
        "called_at": now,
        "counter": counter_name,
    })

    current_key = f"current_number:{service}"
    number = r.hget(ticket_key, "number")
    r.set(current_key, number)

    # 統計
    wait_seconds = now - created_at
    date_str = datetime.fromtimestamp(now).strftime("%Y%m%d")
    stats_key = f"stats:{date_str}:{service}:{counter_name}"
    stats_service_key = f"stats:{date_str}:{service}:ALL"
    
    pipe = r.pipeline()
    pipe.hincrby(stats_key, "count", 1)
    pipe.hincrby(stats_key, "total_wait", wait_seconds)
    pipe.hincrby(stats_service_key, "count", 1)
    pipe.hincrby(stats_service_key, "total_wait", wait_seconds)
    pipe.execute()

    ticket_info = {
        "ticket_id": int(ticket_id),
        "number": int(number),
        "service": service,
        "counter": counter_name,
        "called_at": now,
    }

    channel = f"channel:queue_update:{service}"
    r.publish(channel, json.dumps(ticket_info))

    return ticket_info

def cancel_ticket(ticket_id: int) -> bool:
    ticket_key = f"ticket:{ticket_id}"
    if not r.exists(ticket_key):
        return False
    r.hset(ticket_key, "status", "cancelled")
    return True

# ---------------------------------------------------------
# get_ticket_status: 取得票券狀態
# ---------------------------------------------------------
def get_ticket_status(ticket_id: int) -> dict | None:
    ticket_key = f"ticket:{ticket_id}"
    if not r.exists(ticket_key):
        return None
    data = r.hgetall(ticket_key)
    service = data["service"]
    status = data["status"]

    ahead_count = 0
    if status == "waiting":
        try:
            my_created = float(data["created_at"])
            query = f"@service:{service} @status:{{waiting}} @created_at:[-inf {my_created - 0.001}]"
            
            res = r.execute_command("FT.SEARCH", "idx:ticket", query, "LIMIT", "0", "0")
            
            ahead_count = res[0]
        except Exception as e:
            print("Search error:", e)
            ahead_count = 0 
            
    current_key = f"current_number:{service}"
    current_number = r.get(current_key)

    result = {
        "ticket_id": int(ticket_id),
        "number": int(data["number"]),
        "service": service,
        "status": status,
        "created_at": int(data["created_at"]),
        "called_at": int(data["called_at"]) if data.get("called_at") else None,
        "counter": data.get("counter", ""),
        "ahead_count": ahead_count,
        "current_number": int(current_number) if current_number else None,
        "line_user_id": data.get("line_user_id", "")
    }
    return result

def get_stats_for_date(date_str: str) -> list[dict]:
    pattern = f"stats:{date_str}:*"
    results: list[dict] = []
    for key in r.scan_iter(pattern):
        parts = key.split(":")
        if len(parts) < 4: continue
        _, _, service, counter = parts
        data = r.hgetall(key)
        count = int(data.get("count", 0))
        total_wait = int(data.get("total_wait", 0))
        avg_wait = total_wait / count if count > 0 else 0
        results.append({
            "service": service, "counter": counter,
            "count": count, "avg_wait_seconds": avg_wait,
        })
    return results

# ---------------------------------------------------------
# get_live_queue_stats: 取得即時統計 (已增加 500 錯誤的容錯)
# ---------------------------------------------------------
def get_live_queue_stats() -> list[dict]:
    try:
        raw = r.execute_command(
            "FT.AGGREGATE", "idx:ticket",
            "@status:{waiting|serving}",
            "GROUPBY", 2, "@service", "@status",
            "REDUCE", "COUNT", 0, "AS", "cnt",
        )
        stats: list[dict] = []
        if not raw or raw[0] == 0: return stats
        for row in raw[1:]:
            row_dict = {}
            for i in range(0, len(row), 2):
                row_dict[row[i]] = row[i + 1]
            stats.append({
                "service": row_dict.get("service", ""),
                "status": row_dict.get("status", ""),
                "count": int(row_dict.get("cnt", 0)),
            })
        return stats
    except redis.exceptions.ResponseError as e:
        print(f"🔴 ERROR: RediSearch FT.AGGREGATE failed. Is the RediSearch module loaded? Error: {e}")
        return []
    except Exception as e:
        print(f"🔴 ERROR: Unknown error during FT.AGGREGATE. Error: {e}")
        return []

# queue_core.py (只修改 get_overall_summary 部分)

# ---------------------------------------------------------
# get_overall_summary: 取得總體系統數據 (含 Waiting, Serving, Done, Cancelled)
# ---------------------------------------------------------
def get_overall_summary() -> dict:
    try:
        # 1. 使用 FT.SEARCH 分別計算四種狀態的數量
        # 等待中
        res_waiting = r.execute_command("FT.SEARCH", "idx:ticket", "@status:{waiting}", "LIMIT", "0", "0")
        count_waiting = res_waiting[0] if res_waiting else 0

        # 服務中
        res_serving = r.execute_command("FT.SEARCH", "idx:ticket", "@status:{serving}", "LIMIT", "0", "0")
        count_serving = res_serving[0] if res_serving else 0

        # [新增] 已完成/過號 (status=done)
        res_done = r.execute_command("FT.SEARCH", "idx:ticket", "@status:{done}", "LIMIT", "0", "0")
        count_done = res_done[0] if res_done else 0

        # [新增] 已取消 (status=cancelled)
        res_cancelled = r.execute_command("FT.SEARCH", "idx:ticket", "@status:{cancelled}", "LIMIT", "0", "0")
        count_cancelled = res_cancelled[0] if res_cancelled else 0

        # 2. 讀取總發出票數
        total_issued = int(r.get("ticket:global:id") or 0)
        
        # 3. 讀取今日服務統計 (Hash)
        today_str = datetime.now().strftime("%Y%m%d")
        total_served_today_data = r.hgetall(f"stats:{today_str}:register:ALL")
        
        total_served_today = int(total_served_today_data.get("count", 0) or 0)
        total_wait_time = int(total_served_today_data.get("total_wait", 0) or 0)
        
        avg_wait_time_sec = total_wait_time / total_served_today if total_served_today > 0 else 0

        return {
            "total_issued": total_issued,
            "live_waiting": count_waiting,
            "live_serving": count_serving,
            "live_done": count_done,           # 新增回傳
            "live_cancelled": count_cancelled, # 新增回傳
            "total_served_today": total_served_today,
            "avg_wait_time_today": avg_wait_time_sec,
            "error": None
        }
    except redis.exceptions.ResponseError as e:
        # 發生錯誤時的預設回傳
        return {
            "error": f"RediSearch Error: {str(e)}",
            "total_issued": int(r.get("ticket:global:id") or 0),
            "live_waiting": 0, "live_serving": 0, "live_done": 0, "live_cancelled": 0,
            "total_served_today": 0, "avg_wait_time_today": 0
        }
    except Exception as e:
        print(f"🔴 ERROR: Unknown error in overall summary. Error: {e}")
        return {"error": f"Unknown: {str(e)}", "total_issued": 0}


    try:
        # 1. 改用 FT.SEARCH 直接計算數量 (比 Aggregate 更穩定)
        # 查詢 "waiting" 狀態的總數 (LIMIT 0 0 代表只取數量，不取內容，速度極快)
        res_waiting = r.execute_command("FT.SEARCH", "idx:ticket", "@status:{waiting}", "LIMIT", "0", "0")
        count_waiting = res_waiting[0] if res_waiting else 0

        # 查詢 "serving" 狀態的總數
        res_serving = r.execute_command("FT.SEARCH", "idx:ticket", "@status:{serving}", "LIMIT", "0", "0")
        count_serving = res_serving[0] if res_serving else 0

        # 查詢 "cancelled" 狀態的總數 (選用)
        res_cancelled = r.execute_command("FT.SEARCH", "idx:ticket", "@status:{cancelled}", "LIMIT", "0", "0")
        count_cancelled = res_cancelled[0] if res_cancelled else 0

        # 2. 讀取總發出票數 (用計數器)
        total_issued = int(r.get("ticket:global:id") or 0)
        
        # 3. 讀取今日服務統計 (用於平均時間)
        today_str = datetime.now().strftime("%Y%m%d")
        total_served_today_data = r.hgetall(f"stats:{today_str}:register:ALL")
        
        total_served_today = int(total_served_today_data.get("count", 0) or 0)
        total_wait_time = int(total_served_today_data.get("total_wait", 0) or 0)
        
        avg_wait_time_sec = total_wait_time / total_served_today if total_served_today > 0 else 0

        return {
            "total_issued": total_issued,
            "live_waiting": count_waiting,
            "live_serving": count_serving,
            "total_cancelled": count_cancelled,
            "total_served_today": total_served_today,
            "avg_wait_time_today": avg_wait_time_sec,
            "error": None
        }
    except redis.exceptions.ResponseError as e:
        print(f"🔴 ERROR: RediSearch SEARCH failed. Error: {e}")
        # 如果搜尋失敗，回傳 0 而不是錯誤，讓介面保持顯示
        return {
            "error": f"Search Error: {str(e)}",
            "total_issued": int(r.get("ticket:global:id") or 0),
            "live_waiting": 0, "live_serving": 0, "total_served_today": 0, "avg_wait_time_today": 0
        }
    except Exception as e:
        print(f"🔴 ERROR: Unknown error in overall summary. Error: {e}")
        return {"error": f"Unknown: {str(e)}", "total_issued": 0, "live_waiting": 0, "live_serving": 0, "total_served_today": 0, "avg_wait_time_today": 0}
# ---------------------------------------------------------
# get_hourly_demand: 取得時段熱度分析 (已修復解析錯誤)
# ---------------------------------------------------------
def get_hourly_demand() -> list[dict]:
    try:
        raw = r.execute_command(
            "FT.AGGREGATE", "idx:ticket", "*", 
            "APPLY", "FLOOR((@created_at / 3600) % 24)", "AS", "hour", 
            "GROUPBY", 1, "@hour", 
            "REDUCE", "COUNT", 0, "AS", "total",
            "SORTBY", 2, "@hour", "ASC"
        )
        
        hourly_data = []
        if raw and raw[0] > 0:
            for i in range(1, len(raw)):
                row = raw[i]
                group_data = {}
                for j in range(0, len(row), 2):
                    group_data[row[j]] = row[j+1]
                
                # [關鍵修正] 提供安全預設值 0 進行轉換
                hourly_data.append({
                    "hour": int(group_data.get('@hour', group_data.get('hour', 0))), # 兼容 @hour 和 hour
                    "count": int(group_data.get('total', 0))
                })

        return hourly_data
    except redis.exceptions.ResponseError as e:
        return {"error": "RediSearch Module Failure"}
    except Exception as e:
        return {"error": "Unknown backend error"}