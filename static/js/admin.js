const API_CALL_NEXT = "/counter/register/next";
const API_SUMMARY = "/admin/api/summary";
const API_DEMAND = "/admin/api/demand";

function displayError(elementId, message) {
  const el = document.getElementById(elementId);
  if (el) {
    el.style.display = "block";
    el.textContent = message;
  }
}

function hideError(elementId) {
  const el = document.getElementById(elementId);
  if (el) el.style.display = "none";
}

async function loadSummaryData() {
  try {
    const summaryRes = await fetch(API_SUMMARY);
    if (!summaryRes.ok) throw new Error("Server Error");
    const summaryData = await summaryRes.json();

    if (summaryData.error) {
      displayError("live-error", summaryData.error);
      document.getElementById("live-waiting").textContent = "N/A";
      document.getElementById("live-serving").textContent = "N/A";
      document.getElementById("live-done").textContent = "N/A";
      document.getElementById("live-cancelled").textContent = "N/A";
    } else {
      hideError("live-error");
      document.getElementById("live-waiting").textContent =
        summaryData.live_waiting;
      document.getElementById("live-serving").textContent =
        summaryData.live_serving;
      document.getElementById("live-done").textContent = summaryData.live_done;
      document.getElementById("live-cancelled").textContent =
        summaryData.live_cancelled;
    }

    document.getElementById("total-issued").textContent =
      summaryData.total_issued || 0;
    document.getElementById("total-served-today").textContent =
      summaryData.total_served_today || 0;

    // [關鍵修正] 嚴格檢查 avgTime 是否為有效數字
    const avgTime = summaryData.avg_wait_time_today;

    // 判斷邏輯：不是 undefined, 不是 null, 且是數字型態 (Number)
    if (
      avgTime !== undefined &&
      avgTime !== null &&
      typeof avgTime === "number"
    ) {
      document.getElementById("avg-wait-time-today").textContent =
        avgTime.toFixed(1) + " 秒";
    } else {
      document.getElementById("avg-wait-time-today").textContent = "-- 秒";
    }

    document.getElementById("live-stats-time").textContent =
      new Date().toLocaleTimeString();
  } catch (e) {
    console.error(e);
    displayError("live-error", "連線失敗");
  }
}

async function loadHourlyDemand() {
  const tbody = document.getElementById("hourly-demand-body");
  try {
    const demandRes = await fetch(API_DEMAND);
    if (!demandRes.ok) throw new Error("Server Error");
    const demandData = await demandRes.json();

    if (demandData.error || demandData.length === 0) {
      // 這裡不顯示紅字錯誤，改為顯示柔和的提示
      // displayError('demand-error', demandData.error || '');
      hideError("demand-error"); // 隱藏錯誤框
      tbody.innerHTML = `<tr><td colspan="2" class="text-center text-muted">目前無時段數據</td></tr>`;
      return;
    }

    hideError("demand-error");
    tbody.innerHTML = "";
    demandData.forEach((item) => {
      const tr = document.createElement("tr");
      const hourLabel = item.hour.toString().padStart(2, "0") + ":00";
      tr.innerHTML = `<td>${hourLabel}</td><td>${item.count} 人</td>`;
      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error(e);
    displayError("demand-error", "載入失敗");
  }
}

document.getElementById("btn-call-next").addEventListener("click", async () => {
  const service = document.getElementById("service-select").value;
  const counter =
    document.getElementById("counter-input").value.trim() || "counter-1";
  const pre = document.getElementById("last-call-result");

  try {
    pre.textContent = "呼叫中...";
    const res = await fetch(API_CALL_NEXT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ counter: counter, service: service }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();

    if (data.message === "no one in queue") {
      pre.textContent = `目前 ${service} 沒有人在排隊。`;
    } else {
      pre.textContent = `叫到：${data.number} (櫃台 ${data.counter})`;
    }
  } catch (err) {
    console.error(err);
    pre.textContent = "叫號失敗";
  }
  // 稍微延遲一下再刷新，等待後端寫入完成
  setTimeout(() => {
    loadSummaryData();
    loadHourlyDemand();
  }, 500);
});

function initializeDashboard() {
  loadSummaryData();
  loadHourlyDemand();
  setInterval(loadSummaryData, 5000);
  setInterval(loadHourlyDemand, 30000);
}

initializeDashboard();
