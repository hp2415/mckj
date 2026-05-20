(function () {
  "use strict";

  async function tick() {
    try {
      const u = new URL(window.location.href);
      u.searchParams.set("format", "json");
      const r = await fetch(u.toString(), { credentials: "same-origin" });
      const d = await r.json();
      const st = document.getElementById("st");
      if (st) st.textContent = d.status || "—";
      const qmode = document.getElementById("qmode");
      if (qmode) qmode.textContent = d.query_mode || "—";
      const lastMsg = document.getElementById("last_msg");
      if (lastMsg) lastMsg.textContent = d.last_message || "—";
      const lastOk = document.getElementById("last_ok");
      if (lastOk) lastOk.textContent = d.last_success || "—";
      const curTime = document.getElementById("cur_time");
      if (curTime) curTime.textContent = d.cursor_time_ms || "—";
      const curCreate = document.getElementById("cur_create");
      if (curCreate) curCreate.textContent = d.cursor_create_ts_ms || "0";
    } catch (e) {
      const tip = document.getElementById("poll_tip");
      if (tip) tip.textContent = "无法拉取状态（请保持管理后台已登录）";
    }
  }

  let wired = false;

  function boot() {
    if (!document.getElementById("st")) return;
    if (!wired) {
      wired = true;
      setInterval(tick, 2000);
    }
    tick();
  }

  boot();
  document.addEventListener("admin-panel-loaded", boot);
})();
