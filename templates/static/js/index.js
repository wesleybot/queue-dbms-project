let currentTicketId = null;
let currentService = null;
let sseSource = null;
let qrCodeObj = null;

const dom = {
  card: document.getElementById("main-card"),
  currentNum: document.getElementById("current-number"),
  myNum: document.getElementById("my-number"),
  aheadCount: document.getElementById("ahead-count"),
  statusBadge: document.getElementById("status-badge"),
  btnTake: document.getElementById("btn-take"),
  btnCancel: document.getElementById("btn-cancel"),
  qrWrapper: document.getElementById("qrcode-wrapper"),
  qrBox: document.getElementById("qrcode"),
  counterArea: document.getElementById("counter-area"),
  counterName: document.getElementById("counter-name"),
};

function setNoTicketUI() {
  dom.card.classList.remove("serving-mode");
  dom.myNum.textContent = "-";
  dom.aheadCount.textContent = "-";

  dom.statusBadge.textContent = "尚未取號";
  dom.statusBadge.className = "status-pill bg-light text-muted";

  dom.btnTake.disabled = false;
  dom.btnTake.innerHTML = '<i class="fa-solid fa-plus"></i> 我要抽號';
  dom.btnTake.className = "btn-action btn-primary-custom";

  dom.btnCancel.classList.add("hidden");
  dom.qrWrapper.classList.add("hidden");
  dom.counterArea.classList.add("hidden");

  dom.qrBox.innerHTML = "";
  qrCodeObj = null;
  currentTicketId = null;
  currentService = null;
}

function generateQRCode(text) {
  if (dom.qrBox.innerHTML.trim() !== "") return;
  dom.qrWrapper.classList.remove("hidden");
  qrCodeObj = new QRCode(dom.qrBox, {
    text: text,
    width: 128,
    height: 128,
    colorDark: "#1f2937",
    colorLight: "#ffffff",
    correctLevel: QRCode.CorrectLevel.H,
  });
}

function applyStatusToUI(data) {
  const currentNum = data.current_number ?? 0;
  const myNum = data.number;

  dom.currentNum.textContent = data.current_number ?? "尚未開始";
  dom.myNum.textContent = myNum;
  dom.aheadCount.textContent = data.ahead_count;

  const ticketUrl = `${window.location.origin}/ticket/${data.ticket_id}/view`;
  generateQRCode(ticketUrl);

  let displayStatus = data.status;
  if (data.status === "serving" && currentNum > myNum) {
    displayStatus = "done";
  }

  dom.card.classList.remove("serving-mode");
  dom.counterArea.classList.add("hidden");
  dom.btnCancel.classList.add("hidden");

  if (displayStatus === "waiting") {
    dom.statusBadge.textContent = "排隊中 Waiting";
    dom.statusBadge.className = "status-pill bg-warning text-dark";

    dom.btnTake.disabled = true;
    dom.btnTake.innerHTML = '<i class="fa-solid fa-clock"></i> 已在隊伍中';
    dom.btnTake.className = "btn-action btn-light text-muted";

    dom.btnCancel.classList.remove("hidden");
  } else if (displayStatus === "serving") {
    dom.card.classList.add("serving-mode");

    dom.statusBadge.textContent = "服務中 Serving";
    dom.statusBadge.className = "status-pill bg-success text-white shadow";

    dom.counterArea.classList.remove("hidden");
    dom.counterName.textContent = data.counter || "櫃台";

    dom.btnTake.disabled = true;
    dom.btnTake.innerHTML = "正在服務您";
    dom.btnTake.className = "btn-action btn-success";
  } else {
    dom.statusBadge.textContent = "已過號 / 已取消";
    dom.statusBadge.className = "status-pill bg-secondary text-white";

    dom.btnTake.disabled = false;
    dom.btnTake.innerHTML = '<i class="fa-solid fa-rotate-right"></i> 重新抽號';
    dom.btnTake.className = "btn-action btn-primary-custom";
  }
}

async function refreshStatus() {
  if (!currentTicketId) return;
  try {
    const res = await fetch(`/ticket/${currentTicketId}/status`);
    if (!res.ok) {
      setNoTicketUI();
      setupSSE("register");
      return;
    }
    const data = await res.json();
    applyStatusToUI(data);
  } catch (err) {
    console.error(err);
  }
}

function setupSSE(service) {
  if (
    sseSource &&
    sseSource.url.includes(service) &&
    sseSource.readyState !== EventSource.CLOSED
  )
    return;
  if (sseSource) sseSource.close();

  sseSource = new EventSource(`/events/${service}`);
  sseSource.onmessage = function (event) {
    const msg = JSON.parse(event.data);
    if (msg.number) dom.currentNum.textContent = msg.number;
    if (currentTicketId) refreshStatus();
  };
  let retryCount = 0;
  sseSource.onerror = function (err) {
    sseSource.close();
    let timeout = Math.min(1000 * Math.pow(2, retryCount), 10000);
    retryCount++;
    setTimeout(() => setupSSE(service), timeout);
  };
  sseSource.onopen = function () {
    retryCount = 0;
  };
}

async function init() {
  try {
    const res = await fetch("/session/status");
    const data = await res.json();
    if (data.has_ticket) {
      currentTicketId = data.ticket_id;
      currentService = data.service;
      await refreshStatus();
      setupSSE(currentService);
    } else {
      setNoTicketUI();
      setupSSE("register");
    }
  } catch (err) {
    setNoTicketUI();
    setupSSE("register");
  }
}

dom.btnTake.addEventListener("click", async () => {
  dom.btnTake.disabled = true;
  dom.btnTake.innerHTML =
    '<i class="fa-solid fa-spinner fa-spin"></i> 處理中...';
  try {
    if (currentTicketId) {
      await fetch("/session/clear", { method: "POST" });
      currentTicketId = null;
      dom.qrBox.innerHTML = "";
    }
    const res = await fetch("/session/ticket", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ service: "register" }),
    });
    if (res.ok || res.status === 400) {
      const data = await res.json();
      currentTicketId = data.ticket_id;
      currentService = data.service || "register";
      setupSSE(currentService);
      await refreshStatus();
    } else {
      alert("取號失敗");
      if (!currentTicketId) {
        setNoTicketUI();
        setupSSE("register");
      }
    }
  } catch (e) {
    console.error(e);
    alert("連線錯誤");
    if (!currentTicketId) {
      dom.btnTake.disabled = false;
      dom.btnTake.innerHTML = '<i class="fa-solid fa-plus"></i> 我要抽號';
    }
  }
});

dom.btnCancel.addEventListener("click", async () => {
  if (!confirm("確定要放棄排隊嗎？")) return;
  await fetch("/session/cancel", { method: "POST" });
  setNoTicketUI();
  setupSSE("register");
});

init();
setInterval(() => {
  refreshStatus();
}, 5000);
