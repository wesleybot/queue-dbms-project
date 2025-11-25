# queue_core.py
import time
import json
import redis
from datetime import datetime

# 建立 Redis 連線
r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# 確保 Index 存在 (同之前，略過不變)
try:
    r.execute_command(
        "FT.CREATE", "idx:ticket", "ON", "HASH", "PREFIX", "1", "ticket:",
        "SCHEMA", "service", "TEXT", "status", "TAG", "created_at", "NUMERIC", "SORTABLE"
    )
except redis.exceptions.ResponseError:
    pass

# ---------------------------------------------------------
# 修改點 1: create_ticket 增加 line_user_id 參數
# ---------------------------------------------------------
def create_ticket(service: str, line_user_id: str = "") -> dict:
    pipe = r.pipeline()
    ticket_id = r.incr("ticket:global:id")
    
    number = ticket_id
    now = int(time.time())
    ticket_key = f"ticket:{ticket_id}"
    stream_key = f"queue_stream:{service}"

    # 準備寫入 Hash
    mapping_data = {
        "number": number,
        "service": service,
        "status": "waiting",
        "created_at": now,
        "called_at": "",
        "counter": "",
        # 這裡新增記錄 LINE User ID，方便叫號時反查
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
# call_next 不需要改參數，但它發出的 Pub/Sub 訊息會被 app.py 監聽
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

    # 統計 (Pipeline)
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

    # 發送 Pub/Sub
    channel = f"channel:queue_update:{service}"
    r.publish(channel, json.dumps(ticket_info))

    return ticket_info

# (其他函式 cancel_ticket, get_ticket_status 等保持不變，請直接沿用之前的)
def cancel_ticket(ticket_id: int) -> bool:
    ticket_key = f"ticket:{ticket_id}"
    if not r.exists(ticket_key):
        return False
    r.hset(ticket_key, "status", "cancelled")
    return True

def get_ticket_status(ticket_id: int) -> dict | None:
    ticket_key = f"ticket:{ticket_id}"
    if not r.exists(ticket_key):
        return None
    data = r.hgetall(ticket_key)
    service = data["service"]
    status = data["status"]

    # 這裡我們為了效能，還是保留 RediSearch 的查詢方式
    ahead_count = 0
    if status == "waiting":
        try:
            my_created = float(data["created_at"]) # 注意轉型
            # 查詢比我早掛號且還在 waiting 的人
            query = f"@service:{service} @status:{{waiting}} @created_at:[-inf {my_created - 0.001}]"
            res = r.execute_command("FT.SEARCH", "idx:ticket", query, "LIMIT", "0", "0")
            ahead_count = res[0]
        except Exception as e:
            print("Search error:", e)
            ahead_count = -1 
            
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

def get_live_queue_stats() -> list[dict]:
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