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
    get_stats_for_date, cancel_ticket, get_live_queue_stats, get_overall_summary, get_hourly_demand, r # 確保匯入了所有新函式
)

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-me"

# 環境變數處理
channel_secret = os.environ.get("LINE_CHANNEL_SECRET", "")
channel_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = channel_secret.strip() if channel_secret else None
LINE_CHANNEL_ACCESS_TOKEN = channel_token.strip() if channel_token else None

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

# Redis Session 連線設定
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
            try:
                self.listeners[i].put_nowait(msg)
            except queue.Full:
                del self.listeners[i]

announcer = MessageAnnouncer()

# 背景執行緒：監聽 Redis 並轉發給廣播器
def redis_listener_worker():
    if REDIS_URL:
        pubsub_r = redis.from_url(REDIS_URL, decode_responses=True)
    else:
        pubsub_r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
        
    pubsub = pubsub_r.pubsub()
    pubsub.psubscribe("channel:queue_update:*")
    
    print("🟢 [System] Global Redis Listener Started (Multiplexing Mode)", flush=True)

    for message in pubsub.listen():
        if message["type"] == "pmessage":
            try:
                data_str = message["data"]
                sse_msg = f"data: {data_str}\n\n"
                announcer.announce(sse_msg)
                
                ticket_data = json.loads(data_str)
                handle_push_notification(ticket_data)
            except Exception as e:
                print(f"🔴 Push Error: {e}", flush=True)

def handle_push_notification(ticket_data):
    ticket_id = ticket_data["ticket_id"]
    number = ticket_data["number"]
    counter = ticket_data["counter"]
    
    # 去重鎖
    dedup_key = f"dedup:push:{ticket_id}:{number}"
    is_first_handler = r.set(dedup_key, "1", ex=60, nx=True)
    
    if not is_first_handler:
        return

    ticket_detail = r.hgetall(f"ticket:{ticket_id}")
    line_user_id = ticket_detail.get("line_user_id")

    if line_user_id and line_bot_api:
        print(f"🔔 [Push] Sending to {line_user_id}", flush=True)
        push_text = f"📢 號碼到囉！\n\n您的號碼：{number}\n請前往：{counter}\n請準備好您的手機畫面，盡速前往櫃台辦理。"
        try:
            line_bot_api.push_message(line_user_id, TextSendMessage(text=push_text))
        except LineBotApiError as e:
            print(f"🔴 Push failed: {e}", flush=True)

# 啟動全域監聽執行緒
if not any(t.name == "GlobalRedisListener" for t in threading.enumerate()):
    t = threading.Thread(target=redis_listener_worker, daemon=True, name="GlobalRedisListener")
    t.start()


# ------------------ LINE Webhook (保持不變) ------------------

@app.route("/line/webhook", methods=["POST"])
def line_webhook():
    if not handler or not line_bot_api:
        abort(500)
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_line_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text in ["我要抽號", "抽號", "取號", "我要取號"]:
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
                    is_actually_waiting = False
            else:
                clear_line_user_ticket(user_id)
                is_actually_waiting = False

        if is_actually_waiting:
            status = get_ticket_status(bound["ticket_id"])
            if status["status"] == "serving":
                msg = f"🔔 輪到您囉！\n您的號碼：{status['number']}\n請前往：{status['counter']}"
            else:
                msg = f"您已經在排隊中囉！\n- 您的號碼：{status['number']}\n- 前面還有：{status['ahead_count']} 人"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        else:
            ticket = create_ticket("register", line_user_id=user_id)
            bind_line_user_to_ticket(user_id, ticket["ticket_id"], ticket["service"])
            view_url = url_for("ticket_view", ticket_id=ticket["ticket_id"], _external=True)
            msg = f"🎉 取號成功！\n- 您的號碼：{ticket['number']}\n當叫到您的號碼時，LINE 會自動發送通知給您。\n\n線上查看進度：\n{view_url}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))

    elif text in ["查詢目前叫到號碼", "查詢", "查詢號碼", "查詢進度"]:
        bound = get_line_user_ticket(user_id)
        if bound:
            status = get_ticket_status(bound["ticket_id"])
            if not status:
                clear_line_user_ticket(user_id)
                msg = "您目前沒有排隊中的號碼。輸入「我要抽號」來取票。"
            else:
                my_num = status["number"]
                current_num = status.get("current_number") or 0
                db_status = status["status"]
                is_passed = (db_status == "serving" and current_num > my_num)
                
                if db_status == "waiting":
                    msg = f"📊 排隊狀態：\n- 目前叫到：{current_num}\n- 您的號碼：{my_num}\n- 前面還有：{status['ahead_count']} 人"
                elif db_status == "serving" and not is_passed:
                    msg = f"🔔 您的號碼 {my_num} 正在服務中！\n請前往櫃台: {status['counter']}"
                else:
                    clear_line_user_ticket(user_id)
                    msg = f"您的號碼 {my_num} 服務已結束或已過號 (目前叫到：{current_num})。\n若需重新排隊，請輸入「我要抽號」。"
        else:
            service = "register"
            current_num = r.get(f"current_number:{service}")
            current_num = int(current_num) if current_num else "尚未開始"
            msg = f"您目前沒有取號。\n目前大廳叫號：{current_num}\n若要加入排隊，請輸入「我要抽號」。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))

    elif text in ["取消排隊", "取消"]:
        bound = get_line_user_ticket(user_id)
        if not bound:
            msg = "您目前沒有排隊喔。"
        else:
            cancel_ticket(bound["ticket_id"])
            clear_line_user_ticket(user_id)
            msg = "已為您取消排隊。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入：我要抽號、查詢、或取消。"))

# ------------------ Route ------------------
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
        if request.form.get("username") == "admin" and request.form.get("password") == "1234":
            session["admin_logged_in"] = True
            session["admin_name"] = "admin"
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
    if not status: return render_template("ticket_forbidden.html"), 404
    session_ticket = session.get("ticket_id")
    if session_ticket and int(session_ticket) != ticket_id:
        return render_template("ticket_forbidden.html")
    current_num = status.get("current_number") or 0
    my_num = status["number"]
    is_passed = (status["status"] == "serving" and current_num > my_num)
    if status["status"] in ["done", "cancelled"] or is_passed:
        return render_template("ticket_expired.html", number=my_num, status=status["status"])
    return render_template("ticket_view.html", ticket_id=ticket_id, service=status["service"])

@app.route("/counter/<service>/next", methods=["POST"])
def api_call_next(service):
    data = request.get_json(silent=True) or {}
    counter_name = data.get("counter", "counter-1")
    ticket = call_next(service, counter_name)
    if not ticket: return jsonify({"message": "no one in queue"}), 200
    return jsonify(ticket)

# -------------------------------------------------------------
# [關鍵修正] API 路由 (確保返回 JSON，避免前端崩潰)
# -------------------------------------------------------------
@app.route("/admin/api/summary", methods=["GET"])
def api_admin_summary():
    if not session.get("admin_logged_in"): return jsonify({"error": "unauthorized"}), 401
    try:
        summary = get_overall_summary()
        return jsonify(summary)
    except Exception as e:
        # 捕捉所有錯誤，並返回 JSON 格式的錯誤報告
        return jsonify({"error": f"Backend Error during Summary API: {str(e)}", "total_issued": 0, "live_waiting": "N/A"}), 500

@app.route("/admin/api/demand", methods=["GET"])
def api_admin_demand():
    if not session.get("admin_logged_in"): return jsonify({"error": "unauthorized"}), 401
    try:
        demand = get_hourly_demand()
        return jsonify(demand)
    except Exception as e:
        # 捕捉所有錯誤，並返回 JSON 格式的錯誤報告
        return jsonify({"error": f"Backend Error during Demand API: {str(e)}"}), 500


@app.route("/admin/stats/live")
def admin_stats_live():
    # 舊的路由，保持不動或移除
    if not session.get("admin_logged_in"): return jsonify({"error": "unauthorized"}), 401
    stats = get_live_queue_stats()
    return jsonify(stats)

@app.route("/admin/stats/today-summary", methods=["GET"])
def admin_stats_today_summary():
    # 舊的路由，保持不動或移除
    if not session.get("admin_logged_in"): return jsonify({"error": "unauthorized"}), 401
    today_str = datetime.now().strftime("%Y%m%d")
    stats = get_stats_for_date(today_str)
    service_rows = [row for row in stats if row["counter"] == "ALL"]
    total_count = sum(row["count"] for row in service_rows)
    overall_avg = sum(row["avg_wait_seconds"] * row["count"] for row in service_rows) / total_count if total_count else 0
    return jsonify({"date": today_str, "total_count": total_count, "overall_avg_wait_seconds": overall_avg, "services": service_rows})

@app.route("/events/<service>")
def events(service):
    def generate():
        current_num = r.get(f"current_number:{service}")
        if current_num:
            init_data = json.dumps({"ticket_id": 0, "number": int(current_num), "service": service, "counter": "", "status": "update"})
            yield f"data: {init_data}\n\n"
        
        # [關鍵修正] 確保 SSE 仍能使用 r.get
        if REDIS_URL:
            sse_r = redis.from_url(REDIS_URL, decode_responses=True)
        else:
            sse_r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
            
        pubsub = sse_r.pubsub()
        channel = f"channel:queue_update:{service}"
        pubsub.subscribe(channel)
        
        for message in pubsub.listen():
            if message["type"] == "message":
                yield f"data: {message['data']}\n\n"
    return Response(generate(), mimetype="text/event-stream")

@app.route("/session/status", methods=["GET"])
def session_status():
    return jsonify({"has_ticket": bool(session.get("ticket_id")), "ticket_id": session.get("ticket_id"), "service": session.get("service")})

@app.route("/session/ticket", methods=["POST"])
def session_create_ticket():
    if session.get("ticket_id"): return jsonify({"error": "already_has_ticket", "ticket_id": session["ticket_id"]}), 400
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