# queue_core.py
import time
import json
import os
import redis
import uuid
from datetime import datetime

# ---------------------------------------------------------
# [連線設定]
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
# 核心功能函式
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
# [關鍵修正] call_next: 先將上一位設為 done，再叫下一位
# ---------------------------------------------------------
def call_next(service: str, counter_name: str) -> dict | None:
    
    # ★★★ 新增：自動結案邏輯 ★★★
    # 在叫下一號之前，先搜尋該服務目前正在 "serving" 的票，全部改成 "done"
    try:
        # 這裡搜尋所有 status=serving 的票
        # (如果你希望只關閉自己櫃台的票，可以加 @counter:{counter_name}，但在簡易系統中，清空所有 serving 比較保險)
        query = f"@service:{service} @status:{{serving}}"
        res = r.execute_command("FT.SEARCH", "idx:ticket", query, "LIMIT", "0", "1000")
        
        # FT.SEARCH 回傳格式：[總數, key1, [fields...], key2, [fields...]...]
        if res and res[0] > 0:
            for i in range(1, len(res), 2):
                old_ticket_key = res[i] # 例如 "ticket:10"
                r.hset(old_ticket_key, "status", "done")
                print(f"✅ Auto-completed previous ticket: {old_ticket_key}")
    except Exception as e:
        print(f"⚠️ Cleanup previous tickets failed: {e}")
    # ★★★ 結案邏輯結束 ★★★


    stream_key = f"queue_stream:{service}"
    group_name = "counters_group"
    consumer_name = counter_name

    try:
        r.xgroup_create(stream_key, group_name, id="0", mkstream=True)
    except redis.exceptions.ResponseError:
        pass

    while True:
        messages = r.xreadgroup(group_name, consumer_name, {stream_key: ">"}, count=1)

        if not messages:
            return None
        
        stream_data = messages[0][1]  
        if not stream_data:
            return None

        message_id, data = stream_data[0]
        ticket_id = data["ticket_id"]

        r.xack(stream_key, group_name, message_id)

        ticket_key = f"ticket:{ticket_id}"
        if not r.exists(ticket_key):
            continue

        current_status = r.hget(ticket_key, "status")
        if current_status != "waiting":
            continue

        now = int(time.time())
        r.hset(ticket_key, mapping={
            "status": "serving",
            "called_at": now,
            "counter": counter_name,
        })

        current_key = f"current_number:{service}"
        number = r.hget(ticket_key, "number")
        r.set(current_key, number)

        created_at = int(r.hget(ticket_key, "created_at") or now)
        wait_seconds = now - created_at
        date_str = datetime.fromtimestamp(now).strftime("%Y%m%d")
        
        pipe = r.pipeline()
        pipe.hincrby(f"stats:{date_str}:{service}:{counter_name}", "count", 1)
        pipe.hincrby(f"stats:{date_str}:{service}:{counter_name}", "total_wait", wait_seconds)
        pipe.hincrby(f"stats:{date_str}:{service}:ALL", "count", 1)
        pipe.hincrby(f"stats:{date_str}:{service}:ALL", "total_wait", wait_seconds)
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
            if "no such index" in str(e).lower():
                ensure_index_exists()
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
        results.append({
            "service": service, "counter": counter,
            "count": int(data.get("count", 0)), 
            "avg_wait_seconds": int(data.get("total_wait", 0)) / int(data.get("count", 1)) if int(data.get("count", 0)) > 0 else 0
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
        avg_wait = int(total_data.get("total_wait", 0) or 0) / total_served if total_served > 0 else 0

        return {
            "total_issued": total_issued,
            "live_waiting": res_waiting[0] if res_waiting else 0,
            "live_serving": res_serving[0] if res_serving else 0,
            "live_done": res_done[0] if res_done else 0,
            "live_cancelled": res_cancelled[0] if res_cancelled else 0,
            "total_served_today": total_served,
            "avg_wait_time_today": avg_wait,
            "error": None
        }
    except Exception as e: return {"error": str(e), "total_issued": 0}

def get_hourly_demand() -> list[dict]:
    try:
        raw = r.execute_command("FT.AGGREGATE", "idx:ticket", "*", "APPLY", "FLOOR(((@created_at + 28800) / 3600) % 24)", "AS", "hour", "GROUPBY", 1, "@hour", "REDUCE", "COUNT", 0, "AS", "total", "SORTBY", 2, "@hour", "ASC")
        data = []
        if raw and raw[0] > 0:
            for row in raw[1:]:
                rd = {row[i]: row[i+1] for i in range(0, len(row), 2)}
                data.append({"hour": int(rd.get('hour', 0)), "count": int(rd.get('total', 0))})
        return data
    except: return []