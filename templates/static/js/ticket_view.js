// 透過 Jinja2 取得後端變數
const ticketId = Number("{{ ticket_id }}");
const serviceName = "{{ service }}";
let myNumber = null;

const dom = {
  card: document.getElementById("ticket-card"),
  currentNum: document.getElementById("current-number"),
  myNum: document.getElementById("my-number"),
  aheadCount: document.getElementById("ahead-count"),
  statusBadge: document.getElementById("status-badge"),
  counterArea: document.getElementById("counter-area"),
  counterName: document.getElementById("counter-name"),
};

// 1. 初始化
async function initStatus() {
  try {
    const res = await fetch(`/ticket/${ticketId}/status`);
    if (!res.ok) {
      // 如果票券無效或過期，導向首頁或顯示錯誤
      document.body.innerHTML =
        "<div class='container py-5 text-center'><h3>🚫 票券已失效或不存在</h3><a href='/' class='btn btn-primary mt-3'>返回首頁</a></div>";
      return;
    }
    const data = await res.json();
    myNumber = data.number;
    updateUI(data);
  } catch (e) {
    console.error(e);
  }
}

// 2. UI 更新
function updateUI(data) {
  const currentNum = data.current_number ?? "尚未開始";

  dom.currentNum.textContent = currentNum;
  dom.myNum.textContent = myNumber;

  // 判斷過號 (若目前叫號 > 我的號碼 且 狀態是 serving，視為過號)
  let displayStatus = data.status;
  if (
    data.status === "serving" &&
    typeof currentNum === "number" &&
    currentNum > myNumber
  ) {
    displayStatus = "done";
  }

  // Reset Styles
  dom.card.classList.remove("serving-mode");
  dom.counterArea.style.display = "none";

  // 狀態判斷
  if (displayStatus === "waiting") {
    dom.statusBadge.textContent = "等待中 Waiting";
    dom.statusBadge.className = "status-pill bg-warning text-dark";
    dom.aheadCount.textContent = data.ahead_count;
  } else if (displayStatus === "serving") {
    dom.card.classList.add("serving-mode");

    dom.statusBadge.textContent = "服務中 Serving";
    dom.statusBadge.className = "status-pill bg-success text-white shadow";

    dom.counterArea.style.display = "block";
    dom.counterName.textContent = data.counter || "櫃台";
    dom.aheadCount.textContent = "0"; // 輪到我了

    // 手機震動提示
    if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
  } else if (displayStatus === "done") {
    dom.statusBadge.textContent = "已完成 / 過號";
    dom.statusBadge.className = "status-pill bg-secondary text-white";
    dom.aheadCount.textContent = "0";

    // 如果已完成，可以考慮自動導向過期頁面 (看需求)
    // window.location.href = '/';
  } else if (displayStatus === "cancelled") {
    dom.statusBadge.textContent = "已取消";
    dom.statusBadge.className = "status-pill bg-secondary text-white";
    dom.aheadCount.textContent = "-";
  }
}

// 3. SSE 連線 (即時更新)
const evtSource = new EventSource(`/events/${serviceName}`);

evtSource.onmessage = function (event) {
  const msg = JSON.parse(event.data);
  console.log("SSE Update:", msg);

  // 收到廣播後，立即更新大標題
  if (msg.number) dom.currentNum.textContent = msg.number;

  // 並重新 fetch 詳細狀態 (確保前面人數準確)
  // 稍微延遲一點點，避免後端寫入未完成
  setTimeout(() => {
    fetch(`/ticket/${ticketId}/status`)
      .then((res) => res.json())
      .then((data) => updateUI(data))
      .catch((e) => console.error(e));
  }, 200);
};

// 4. 雙重保險：輪詢
setInterval(() => {
  fetch(`/ticket/${ticketId}/status`)
    .then((res) => {
      if (res.ok) return res.json();
    })
    .then((data) => {
      if (data) updateUI(data);
    })
    .catch(console.error);
}, 5000);

// 啟動
initStatus();
