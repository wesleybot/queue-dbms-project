# app.py
import os
import json
import threading
import queue
import redis
import uuid # 新增-> 為了在 app.py 這端也能補救 Token
from flask import (
    Flask, request, jsonify, send_file,
    url_for, Response, render_template, session, redirect, abort
)
from flask_session import Session
from datetime import datetime
import qrcode
import io

# 引用 queue_core
from queue_core import (
    create_ticket, call_next, get_ticket_status,
    get_stats_for_date, cancel_ticket, get_live_queue_stats, 
    get_overall_summary, get_hourly_demand, r
)

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-me"

# 設定網域
BASE_URL = "https://queue.xiandbms.ggff.net"

# 環境變數
channel_secret = os.environ.get("LINE_CHANNEL_SECRET", "")
channel_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = channel_secret.strip() if channel_secret else None
LINE_CHANNEL_ACCESS_TOKEN = channel_token.strip() if channel_token else None

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

# Redis Session 連線
REDIS_URL = os.environ.get("REDIS_URL")
if REDIS_URL:
    # 使用 connection pool 限制 session 連線數
    session_pool = redis.ConnectionPool.from_url(REDIS_URL, max_connections=3, socket_timeout=5)
    session_redis = redis.Redis(connection_pool=session_pool)
else:
    session_redis = redis.Redis(host="localhost", port=6379, db=0)

app.config["SESSION_TYPE"] = "redis"
app.config["SESSION_REDIS"] = session_redis
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True
Session(app)

# Helper Functions
def bind_line_user_to_ticket(user_id: str, ticket_id: int, service: str):
    key = f"line_user:{user_id}"
    r.hset(key, mapping={"ticket_id": ticket_id, "service": service})

def get_line_user_ticket(user_id: str):
    key = f"line_user:{user_id}"
    if not r.exists(key): return None
    data = r.hgetall(key)
    return {"ticket_id": int(data["ticket_id"]), "service": data["service"]}

def clear_line_user_ticket(user_id: str):
    key = f"line_user:{user_id}"
    r.delete(key)

# 核心架構 廣播系統 (Message Announcer)
class MessageAnnouncer:
    def __init__(self):
        self.listeners = []

    def listen(self):
        q = queue.Queue(maxsize=5)
        self.listeners.append(q)
        return q

    def announce(self, msg):
        # 廣播給所有正在聽的 Queue
        for i in reversed(range(len(self.listeners))):
            try:
                self.listeners[i].put_nowait(msg)
            except queue.Full:
                del self.listeners[i]

# 全域廣播器實例
announcer = MessageAnnouncer()

# 背景執行緒：監聽 Redis 並轉發給廣播器
def redis_listener_worker():
    if REDIS_URL:
        # 這裡使用 decode_responses=True 方便處理字串
        pubsub_r = redis.from_url(REDIS_URL, decode_responses=True)
    else:
        pubsub_r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
        
    pubsub = pubsub_r.pubsub()
    pubsub.psubscribe("channel:queue_update:*")
    
    print("[System] Global Redis Listener Started (Multiplexing Mode)", flush=True)

    for message in pubsub.listen():
        if message["type"] == "pmessage":
            try:
                data_str = message["data"]
                
                # 1. 轉發給廣播器 (服務網頁 SSE)
                sse_msg = f"data: {data_str}\n\n"
                announcer.announce(sse_msg)
                
                # 2. 處理 LINE 推播
                ticket_data = json.loads(data_str)
                handle_push_notification(ticket_data)
            except Exception as e:
                print(f"Push Error: {e}", flush=True)

def handle_push_notification(ticket_data):
    ticket_id = ticket_data["ticket_id"]
    number = ticket_data["number"]
    counter = ticket_data["counter"]
    
    # 去重鎖 (使用全域 r 連線)
    dedup_key = f"dedup:push:{ticket_id}:{number}"
    is_first_handler = r.set(dedup_key, "1", ex=60, nx=True)
    
    if not is_first_handler:
        return

    ticket_detail = r.hgetall(f"ticket:{ticket_id}")
    line_user_id = ticket_detail.get("line_user_id")

    if line_user_id and line_bot_api:
        print(f"[Push] Sending to {line_user_id}", flush=True)
        push_text = f"【@通知 輪到您了】號碼到囉！\n\n您的號碼：{number}\n請前往：{counter}"
        try:
            line_bot_api.push_message(line_user_id, TextSendMessage(text=push_text))
        except Exception:
            pass

# 啟動全域監聽執行緒
if not any(t.name == "GlobalRedisListener" for t in threading.enumerate()):
    t = threading.Thread(target=redis_listener_worker, daemon=True, name="GlobalRedisListener")
    t.start()

# SSE 路由
@app.route("/events/<service>")
def events(service):
    def stream():
        messages = announcer.listen()
        # 傳送初始狀態
        try:
            current_num = r.get(f"current_number:{service}")
            if current_num:
                init_data = json.dumps({"ticket_id": 0, "number": int(current_num), "service": service, "counter": "", "status": "update"})
                yield f"data: {init_data}\n\n"
        except:
            pass

        # 監聽廣播
        while True:
            msg = messages.get()
            yield msg

    return Response(stream(), mimetype="text/event-stream")


# ------------------ LINE Webhook ------------------

@app.route("/line/webhook", methods=["POST"])
def line_webhook():
    if not handler or not line_bot_api: abort(500)
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_line_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text in ["!我要抽號", "抽號", "取號", "我要取號"]:
        bound = get_line_user_ticket(user_id)
        is_actually_waiting = False
        if bound:
            status = get_ticket_status(bound["ticket_id"])
            if status:
                current_num = status.get("current_number") or 0
                my_num = status["number"]
                is_passed = (status["status"] == "serving" and current_num > my_num)
                
                if status["status"] == "waiting":
                    is_actually_waiting = True
                elif status["status"] == "serving" and not is_passed:
                    is_actually_waiting = True
                else:
                    clear_line_user_ticket(user_id)
            else:
                clear_line_user_ticket(user_id)

        if is_actually_waiting:
            st = get_ticket_status(bound["ticket_id"])
            msg = f"您已在排隊中！\n您的號碼：{st['number']}\n前面還有：{st['ahead_count']} 人"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        else:
            ticket = create_ticket("register", line_user_id=user_id)
            bind_line_user_to_ticket(user_id, ticket["ticket_id"], ticket["service"])
            
            # 防呆處理：如果 ticket 字典裡沒有 token，我們現場補救一個
            # 這樣就算 queue_core.py 沒更新成功，這裡也不會報錯
            ticket_token = ticket.get('token')
            if not ticket_token:
                ticket_token = str(uuid.uuid4()) # 補救措施
                print(f"Warning: Token missing in create_ticket response. Generated fallback: {ticket_token}")
                # 嘗試補寫回 Redis (非必要，但保險)
                r.hset(f"ticket:{ticket['ticket_id']}", "token", ticket_token)
            
            # 使用統一網址 + Token
            view_url = f"{BASE_URL}/ticket/{ticket['ticket_id']}/view?token={ticket_token}"
            msg = f"【@通知 取號成功】\n您的號碼：{ticket['number']}\n\n查詢線上進度：\n{view_url}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))

    elif text in ["查詢", "!查詢目前排隊進度"]:
        bound = get_line_user_ticket(user_id)
        if bound:
            status = get_ticket_status(bound["ticket_id"])
            if not status:
                clear_line_user_ticket(user_id)
                msg = "查無票券，請重新抽號。"
            else:
                current_num = status.get("current_number") or 0
                my_num = status["number"]
                is_passed = (status["status"] == "serving" and current_num > my_num)
                
                if status["status"] == "waiting":
                    msg = f"【@通知 排隊狀態】：\n- 目前叫到：{current_num}\n- 您的號碼：{my_num}\n- 前面還有：{status['ahead_count']} 人"
                elif status["status"] == "serving" and not is_passed:
                    msg = f"【@通知 您正在服務中】您的號碼： {my_num} \n請儘速前往櫃台: {status['counter']}"
                else:
                    clear_line_user_ticket(user_id)
                    msg = f"【@通知 服務結束或已過號】\n您的號碼： {my_num} \n目前叫到：{current_num}。\n若需重新排隊，請點取選單或輸入「我要抽號」。"
        else:
            service = "register"
            current_num = r.get(f"current_number:{service}")
            current_num = int(current_num) if current_num else "尚未開始"
            msg = f"【@通知 尚未取號】\n目前大廳叫號：{current_num}\n若要加入排隊，請點取選單或輸入「我要抽號」。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))

    elif text in ["取消", "!取消排隊"]:
        bound = get_line_user_ticket(user_id)
        if bound:
            cancel_ticket(bound["ticket_id"])
            clear_line_user_ticket(user_id)
            msg = "【@通知 已取消排隊】"
        else:
            msg = "【@通知 您沒有排隊】"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))

# ------------------ 前端與 API 路由 ------------------

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/admin", methods=["GET"])
def admin_home():
    if not session.get("admin_logged_in"): return redirect("/admin/login")
    return render_template("admin.html", admin_name=session.get("admin_name", "admin"))

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        # 改從環境變數讀取，如果沒設定則使用預設值 "???" / "???"
        # 這樣在本地測試沒 .env 也不會壞，但在雲端可以設強密碼
        env_username = os.environ.get("ADMIN_USERNAME", "你不要看我")
        env_password = os.environ.get("ADMIN_PASSWORD", "1234")

        if request.form.get("username") == env_username and request.form.get("password") == env_password:
            session["admin_logged_in"] = True
            session["admin_name"] = env_username
            return redirect("/admin")
        else:
            error = "帳號或密碼錯誤"
    return render_template("login.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/")

@app.route("/ticket/<int:ticket_id>/view", methods=["GET"])
def ticket_view(ticket_id):
    status = get_ticket_status(ticket_id)
    # 404 若票不存在
    if not status: return render_template("ticket_forbidden.html"), 404

    # 身分驗證
    session_ticket = session.get("ticket_id")
    url_token = request.args.get("token")
    db_token = status.get("token")
    
    is_authorized = False
    if session_ticket and int(session_ticket) == ticket_id: is_authorized = True
    elif url_token and db_token and url_token == db_token: is_authorized = True
        
    if not is_authorized: return render_template("ticket_forbidden.html")

    # 狀態檢查 (過號)
    current_num = status.get("current_number") or 0
    my_num = status["number"]
    is_passed = (status["status"] == "serving" and current_num > my_num)
    
    if status["status"] in ["done", "cancelled"] or is_passed:
        return render_template("ticket_expired.html", number=my_num, status=status["status"])

    return render_template("ticket_view.html", ticket_id=ticket_id, service=status["service"])

@app.route("/counter/<service>/next", methods=["POST"])
def api_call_next(service):
    data = request.get_json(silent=True) or {}
    # 這裡呼叫的 call_next 已經在 queue_core.py 修復過 (會跳過 cancelled)
    ticket = call_next(service, data.get("counter", "counter-1"))
    if not ticket: return jsonify({"message": "no one in queue"}), 200
    return jsonify(ticket)

# Admin APIs (JSON Only)
@app.route("/admin/api/summary", methods=["GET"])
def api_admin_summary():
    if not session.get("admin_logged_in"): return jsonify({"error": "unauthorized"}), 401
    try:
        return jsonify(get_overall_summary())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin/api/demand", methods=["GET"])
def api_admin_demand():
    if not session.get("admin_logged_in"): return jsonify({"error": "unauthorized"}), 401
    try:
        return jsonify(get_hourly_demand())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Session APIs
@app.route("/session/status", methods=["GET"])
def session_status():
    return jsonify({"has_ticket": bool(session.get("ticket_id")), "ticket_id": session.get("ticket_id"), "service": session.get("service")})

@app.route("/session/ticket", methods=["POST"])
def session_create_ticket():
    if session.get("ticket_id"): return jsonify({"error": "already_has_ticket"}), 400
    ticket = create_ticket("register")
    session["ticket_id"] = ticket["ticket_id"]
    session["service"] = ticket["service"]
    return jsonify(ticket), 201

@app.route("/session/cancel", methods=["POST"])
def session_cancel():
    if session.get("ticket_id"):
        cancel_ticket(session["ticket_id"])
        session.pop("ticket_id", None)
        session.pop("service", None)
    return jsonify({"message": "cancelled"}), 200

@app.route("/session/clear", methods=["POST"])
def session_clear():
    session.pop("ticket_id", None)
    session.pop("service", None)
    return jsonify({"message": "cleared"}), 200

@app.route("/ticket/<int:ticket_id>/status", methods=["GET"])
def api_ticket_status(ticket_id):
    status = get_ticket_status(ticket_id)
    return jsonify(status) if status else (jsonify({"error": "not found"}), 404)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)