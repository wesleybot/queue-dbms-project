# queue_core.py
import time
import json
import os
import redis
import uuid
from datetime import datetime

# ---------------------------------------------------------
# [連線設定] 限制 max_connections=4
# ---------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL")

if REDIS_URL:
    pool = redis.ConnectionPool.from_url(
        REDIS_URL, 
        decode_responses=True, 
        max_connections=4, 
        socket_timeout=5
    )
    r = redis.Redis(connection_pool=pool)
else:
    r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# ---------------------------------------------------------
# 自動確保索引存在
# ---------------------------------------------------------
def ensure_index_exists():
    try:
        r.execute_command(
            "FT.CREATE", "idx:ticket", "ON", "HASH", "PREFIX", "1", "ticket:",
            "SCHEMA", "service", "TEXT", "status", "TAG", "created_at", "NUMERIC", "SORTABLE"
        )
        print("✅ Index 'idx:ticket' created successfully.")
    except redis.exceptions.ResponseError as e:
        if "Index already exists" not in str(e):
            print(f"⚠️ Index creation failed: {e}")

ensure_index_exists()

# ---------------------------------------------------------
# create_ticket
# ---------------------------------------------------------
def create_ticket(service: str, line_user_id: str = "") -> dict:
    pipe = r.pipeline()
    ticket_id = r.incr("ticket:global:id")
    number = ticket_id
    now = int(time.time())
    access_token = str(uuid.uuid4())
    
    ticket_key = f"ticket:{ticket_id}"
    stream_key = f"queue_stream:{service}"

    mapping_data = {
        "number": number,
        "service": service,
        "status": "waiting",
        "created_at": now,
        "called_at": "",
        "counter": "",
        "line_user_id": line_user_id,
        "token": access_token
    }
    
    pipe.hset(ticket_key, mapping=mapping_data)
    pipe.xadd(stream_key, {"ticket_id": ticket_id}, maxlen=1000)
    pipe.execute()

    return {
        "ticket_id": ticket_id,
        "number": number,
        "service": service,
        "created_at": now,
        "token": access_token
    }

# ---------------------------------------------------------
# [關鍵邏輯變更] call_next: 計算服務時間 (Service Time)
# ---------------------------------------------------------
def call_next(service: str, counter_name: str) -> dict | None:
    
    # 1. 自動結案：將上一位 serving 改為 done
    try:
        query = f"@service:{service} @status:{{serving}}"
        res = r.execute_command("FT.SEARCH", "idx:ticket", query, "LIMIT", "0", "1000")
        if res and res[0] > 0:
            for i in range(1, len(res), 2):
                old_ticket_key = res[i]
                r.hset(old_ticket_key, "status", "done")
    except Exception:
        pass

    # 2. 處理 Stream
    stream_key = f"queue_stream:{service}"
    group_name = "counters_group"
    consumer_name = counter_name

    try:
        r.xgroup_create(stream_key, group_name, id="0", mkstream=True)
    except redis.exceptions.ResponseError:
        pass

    while True:
        messages = r.xreadgroup(group_name, consumer_name, {stream_key: ">"}, count=1)
        if not messages: return None
        
        stream_data = messages[0][1]  
        if not stream_data: return None

        message_id, data = stream_data[0]
        ticket_id = data["ticket_id"]

        r.xack(stream_key, group_name, message_id)

        ticket_key = f"ticket:{ticket_id}"
        if not r.exists(ticket_key): continue

        current_status = r.hget(ticket_key, "status")
        if current_status != "waiting": continue

        # --- 叫號成功 ---
        now = int(time.time())
        r.hset(ticket_key, mapping={
            "status": "serving",
            "called_at": now,
            "counter": counter_name,
        })

        current_key = f"current_number:{service}"
        number = r.hget(ticket_key, "number")
        r.set(current_key, number)

        # ★★★ [新邏輯] 計算服務時間 (Service Duration) ★★★
        # 邏輯：這次叫號時間 - 上次叫號時間 = 上一位客人的服務時間
        last_activity_key = f"counter:last_activity:{service}:{counter_name}"
        last_time = r.get(last_activity_key)
        
        # 更新最後活動時間為現在
        r.set(last_activity_key, now)

        today_str = datetime.fromtimestamp(now).strftime("%Y%m%d")
        stats_key = f"stats:{today_str}:{service}:{counter_name}"
        stats_service_key = f"stats:{today_str}:{service}:ALL"
        
        pipe = r.pipeline()
        # 總叫號次數
        pipe.hincrby(stats_key, "count", 1)
        pipe.hincrby(stats_service_key, "count", 1)

        if last_time:
            duration = now - int(last_time)
            # 只有當間隔小於 1 小時才計入 (避免午休拉壞平均)
            if duration < 3600:
                pipe.hincrby(stats_key, "total_svc_time", duration)
                pipe.hincrby(stats_key, "svc_count", 1) # 有效服務次數
                
                pipe.hincrby(stats_service_key, "total_svc_time", duration)
                pipe.hincrby(stats_service_key, "svc_count", 1)
        
        pipe.execute()
        # ★★★ [新邏輯結束] ★★★

        ticket_info = {
            "ticket_id": int(ticket_id),
            "number": int(number),
            "service": service,
            "counter": counter_name,
            "called_at": now,
        }
        r.publish(f"channel:queue_update:{service}", json.dumps(ticket_info))
        return ticket_info

def cancel_ticket(ticket_id: int) -> bool:
    ticket_key = f"ticket:{ticket_id}"
    if not r.exists(ticket_key): return False
    r.hset(ticket_key, "status", "cancelled")
    return True

def get_ticket_status(ticket_id: int) -> dict | None:
    ticket_key = f"ticket:{ticket_id}"
    if not r.exists(ticket_key): return None
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
        except redis.exceptions.ResponseError as e:
            if "no such index" in str(e).lower(): ensure_index_exists()
            ahead_count = 0
        except Exception:
            ahead_count = 0 
            
    current_number = r.get(f"current_number:{service}")

    return {
        "ticket_id": int(ticket_id),
        "number": int(data["number"]),
        "service": service,
        "status": status,
        "created_at": int(data["created_at"]),
        "called_at": int(data.get("called_at", 0)) if data.get("called_at") else None,
        "counter": data.get("counter", ""),
        "ahead_count": ahead_count,
        "current_number": int(current_number) if current_number else None,
        "line_user_id": data.get("line_user_id", ""),
        "token": data.get("token", "")
    }

def get_stats_for_date(date_str: str) -> list[dict]:
    pattern = f"stats:{date_str}:*"
    results: list[dict] = []
    for key in r.scan_iter(pattern):
        parts = key.split(":")
        if len(parts) < 4: continue
        _, _, service, counter = parts
        data = r.hgetall(key)
        
        # [新邏輯] 改用 svc_count 計算平均
        svc_cnt = int(data.get("svc_count", 0))
        total_svc = int(data.get("total_svc_time", 0))
        avg = total_svc / svc_cnt if svc_cnt > 0 else 0
        
        results.append({
            "service": service, "counter": counter,
            "count": int(data.get("count", 0)), 
            "avg_wait_seconds": avg
        })
    return results

def get_live_queue_stats() -> list[dict]:
    try:
        raw = r.execute_command("FT.AGGREGATE", "idx:ticket", "@status:{waiting|serving}", "GROUPBY", 2, "@service", "@status", "REDUCE", "COUNT", 0, "AS", "cnt")
        stats = []
        if raw and raw[0] > 0:
            for row in raw[1:]:
                rd = {row[i]: row[i+1] for i in range(0, len(row), 2)}
                stats.append({"service": rd.get("service"), "status": rd.get("status"), "count": int(rd.get("cnt", 0))})
        return stats
    except redis.exceptions.ResponseError as e:
        if "no such index" in str(e).lower(): ensure_index_exists()
        return []
    except: return []

# ---------------------------------------------------------
# [關鍵修正] get_overall_summary: 改讀取 svc_time
# ---------------------------------------------------------
def get_overall_summary() -> dict:
    try:
        res_waiting = r.execute_command("FT.SEARCH", "idx:ticket", "@status:{waiting}", "LIMIT", "0", "0")
        res_serving = r.execute_command("FT.SEARCH", "idx:ticket", "@status:{serving}", "LIMIT", "0", "0")
        res_done = r.execute_command("FT.SEARCH", "idx:ticket", "@status:{done}", "LIMIT", "0", "0")
        res_cancelled = r.execute_command("FT.SEARCH", "idx:ticket", "@status:{cancelled}", "LIMIT", "0", "0")
        
        total_issued = int(r.get("ticket:global:id") or 0)
        today_str = datetime.now().strftime("%Y%m%d")
        total_data = r.hgetall(f"stats:{today_str}:register:ALL")
        
        total_served = int(total_data.get("count", 0) or 0)
        
        # [新邏輯] 讀取服務時長
        total_svc_time = int(total_data.get("total_svc_time", 0) or 0)
        svc_count = int(total_data.get("svc_count", 0) or 0)
        
        avg_svc_time = total_svc_time / svc_count if svc_count > 0 else 0

        return {
            "total_issued": total_issued,
            "live_waiting": res_waiting[0] if res_waiting else 0,
            "live_serving": res_serving[0] if res_serving else 0,
            "live_done": res_done[0] if res_done else 0,
            "live_cancelled": res_cancelled[0] if res_cancelled else 0,
            "total_served_today": total_served,
            "avg_wait_time_today": avg_svc_time, 
            "error": None
        }
    except Exception as e: return {"error": str(e), "total_issued": 0}

def get_hourly_demand() -> list[dict]:
    try:
        # [時區修正] +8 小時
        raw = r.execute_command("FT.AGGREGATE", "idx:ticket", "*", "APPLY", "FLOOR(((@created_at + 28800) / 3600) % 24)", "AS", "hour", "GROUPBY", 1, "@hour", "REDUCE", "COUNT", 0, "AS", "total", "SORTBY", 2, "@hour", "ASC")
        data = []
        if raw and raw[0] > 0:
            for row in raw[1:]:
                rd = {row[i]: row[i+1] for i in range(0, len(row), 2)}
                data.append({"hour": int(rd.get('hour', 0)), "count": int(rd.get('total', 0))})
        return data
    except: return []