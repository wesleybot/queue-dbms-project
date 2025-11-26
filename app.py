# app.py
import os
import json
import threading
import queue
import redis
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
    get_stats_for_date, cancel_ticket, get_live_queue_stats, get_overall_summary, get_hourly_demand, r
)

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-me"

# ★★★ 設定統一的網域 ★★★
BASE_URL = "https://queue.xiandbms.ggff.net"

channel_secret = os.environ.get("LINE_CHANNEL_SECRET", "")
channel_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = channel_secret.strip() if channel_secret else None
LINE_CHANNEL_ACCESS_TOKEN = channel_token.strip() if channel_token else None

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

# Redis Session
REDIS_URL = os.environ.get("REDIS_URL")
if REDIS_URL:
    session_redis = redis.from_url(REDIS_URL)
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

# 廣播系統
class MessageAnnouncer:
    def __init__(self):
        self.listeners = []
    def listen(self):
        q = queue.Queue(maxsize=5)
        self.listeners.append(q)
        return q
    def announce(self, msg):
        for i in reversed(range(len(self.listeners))):
            try: self.listeners[i].put_nowait(msg)
            except queue.Full: del self.listeners[i]

announcer = MessageAnnouncer()

def redis_listener_worker():
    if REDIS_URL: pubsub_r = redis.from_url(REDIS_URL, decode_responses=True)
    else: pubsub_r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
    pubsub = pubsub_r.pubsub()
    pubsub.psubscribe("channel:queue_update:*")
    print("🟢 [System] Global Redis Listener Started", flush=True)
    for message in pubsub.listen():
        if message["type"] == "pmessage":
            try:
                data_str = message["data"]
                announcer.announce(f"data: {data_str}\n\n")
                handle_push_notification(json.loads(data_str))
            except Exception as e: print(f"🔴 Push Error: {e}", flush=True)

def handle_push_notification(ticket_data):
    ticket_id = ticket_data["ticket_id"]
    number = ticket_data["number"]
    counter = ticket_data["counter"]
    if not r.set(f"dedup:push:{ticket_id}:{number}", "1", ex=60, nx=True): return

    ticket_detail = r.hgetall(f"ticket:{ticket_id}")
    line_user_id = ticket_detail.get("line_user_id")
    if line_user_id and line_bot_api:
        push_text = f"📢 號碼到囉！\n\n您的號碼：{number}\n請前往：{counter}"
        try: line_bot_api.push_message(line_user_id, TextSendMessage(text=push_text))
        except Exception: pass

if not any(t.name == "GlobalRedisListener" for t in threading.enumerate()):
    t = threading.Thread(target=redis_listener_worker, daemon=True, name="GlobalRedisListener")
    t.start()

# ------------------ LINE Webhook (含統一網址 & Token) ------------------
@app.route("/line/webhook", methods=["POST"])
def line_webhook():
    if not handler: abort(500)
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_line_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text in ["我要抽號", "抽號", "取號", "我要取號"]:
        bound = get_line_user_ticket(user_id)
        # 檢查舊票
        is_waiting = False
        if bound:
            status = get_ticket_status(bound["ticket_id"])
            if status:
                is_passed = (status["status"] == "serving" and (status.get("current_number") or 0) > status["number"])
                if status["status"] == "waiting": is_waiting = True
                elif status["status"] == "serving" and not is_passed: is_waiting = True
                else: clear_line_user_ticket(user_id)
            else: clear_line_user_ticket(user_id)

        if is_waiting:
            st = get_ticket_status(bound["ticket_id"])
            msg = f"您已在排隊中！\n號碼：{st['number']}\n前面：{st['ahead_count']} 人"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        else:
            ticket = create_ticket("register", line_user_id=user_id)
            bind_line_user_to_ticket(user_id, ticket["ticket_id"], ticket["service"])
            
            # ★★★ 關鍵：使用 BASE_URL 並加上 Token ★★★
            # 這樣 LINE 使用者點擊時，我們才能驗證他是這個票的主人
            view_url = f"{BASE_URL}/ticket/{ticket['ticket_id']}/view?token={ticket['token']}"
            
            msg = f"🎉 取號成功！\n號碼：{ticket['number']}\n\n線上進度：\n{view_url}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))

    elif text in ["查詢", "查詢進度"]:
        # ... (查詢邏輯省略，與之前相同) ...
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請看上方選單或輸入「我要抽號」"))
    
    elif text in ["取消", "取消排隊"]:
        bound = get_line_user_ticket(user_id)
        if bound:
            cancel_ticket(bound["ticket_id"])
            clear_line_user_ticket(user_id)
            msg = "已取消排隊。"
        else:
            msg = "您沒有排隊喔。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))

# ----------------------------------------------------------------
# [關鍵修正] ticket_view: 嚴格的身分與狀態檢查
# ----------------------------------------------------------------
@app.route("/ticket/<int:ticket_id>/view", methods=["GET"])
def ticket_view(ticket_id):
    status = get_ticket_status(ticket_id)
    
    # 1. 票不存在 -> 404
    if not status: 
        return render_template("ticket_forbidden.html"), 404

    # 2. 身分驗證 (Authorization)
    # 規則：必須滿足以下「其中之一」才放行
    # A. 瀏覽器 Session 中的 ticket_id 與網址相符 (網頁抽號者)
    # B. 網址參數中的 token 與資料庫中的 token 相符 (LINE/QR Code 使用者)
    
    session_ticket = session.get("ticket_id")
    url_token = request.args.get("token")
    db_token = status.get("token")
    
    is_authorized = False
    
    if session_ticket and int(session_ticket) == ticket_id:
        is_authorized = True
    elif url_token and db_token and url_token == db_token:
        is_authorized = True
        
    if not is_authorized:
        return render_template("ticket_forbidden.html")

    # 3. 狀態檢查 (Status Check)
    current_num = status.get("current_number") or 0
    my_num = status["number"]
    is_passed = (status["status"] == "serving" and current_num > my_num)
    
    if status["status"] in ["done", "cancelled"] or is_passed:
        return render_template("ticket_expired.html", number=my_num, status=status["status"])

    # 4. 放行
    return render_template("ticket_view.html", ticket_id=ticket_id, service=status["service"])

# ... (其餘路由 API, admin, events 保持不變) ...
@app.route("/", methods=["GET"])
def index(): return render_template("index.html")
@app.route("/admin", methods=["GET"])
def admin_home(): 
    if not session.get("admin_logged_in"): return redirect("/admin/login")
    return render_template("admin.html", admin_name="admin")
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("username") == "admin" and request.form.get("password") == "1234":
            session["admin_logged_in"] = True
            return redirect("/admin")
    return render_template("login.html")
@app.route("/admin/logout")
def admin_logout(): session.clear(); return redirect("/")
@app.route("/counter/<service>/next", methods=["POST"])
def api_call_next(service):
    data = request.get_json(silent=True) or {}
    t = call_next(service, data.get("counter", "c1"))
    return jsonify(t if t else {"message": "no one"})
@app.route("/admin/api/summary", methods=["GET"])
def api_sum(): 
    if not session.get("admin_logged_in"): return jsonify({}), 401
    return jsonify(get_overall_summary())
@app.route("/admin/api/demand", methods=["GET"])
def api_dem(): 
    if not session.get("admin_logged_in"): return jsonify({}), 401
    return jsonify(get_hourly_demand())
@app.route("/session/status", methods=["GET"])
def sess_stat(): return jsonify({"has_ticket": bool(session.get("ticket_id")), "ticket_id": session.get("ticket_id"), "service": session.get("service")})
@app.route("/session/ticket", methods=["POST"])
def sess_create():
    if session.get("ticket_id"): return jsonify({}), 400
    t = create_ticket("register")
    session["ticket_id"] = t["ticket_id"]; session["service"] = t["service"]
    return jsonify(t), 201
@app.route("/session/cancel", methods=["POST"])
def sess_cancel():
    if session.get("ticket_id"): cancel_ticket(session.get("ticket_id")); session.clear()
    return jsonify({"msg": "ok"})
@app.route("/session/clear", methods=["POST"])
def sess_clear(): session.clear(); return jsonify({"msg": "ok"})
@app.route("/ticket/<int:ticket_id>/status", methods=["GET"])
def api_tick_stat(ticket_id):
    s = get_ticket_status(ticket_id)
    return jsonify(s) if s else (jsonify({}), 404)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)