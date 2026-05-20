(function () {
  "use strict";

  let charts = {};

  function pct(v) {
    return (v * 100).toFixed(1) + "%";
  }
  function fmtInt(n) {
    try {
      return new Intl.NumberFormat().format(n);
    } catch (e) {
      return String(n);
    }
  }

  function setLastUpdated() {
    const el = document.getElementById("last-updated");
    if (el) el.textContent = "最近更新：" + new Date().toLocaleString();
  }

  async function load() {
    const days = document.getElementById("days").value || "7";
    const u = new URL(window.location.href);
    u.searchParams.set("format", "json");
    u.searchParams.set("days", days);
    const r = await fetch(u.toString(), { credentials: "same-origin" });
    const data = await r.json();
    render(data);
    setLastUpdated();
  }

  function renderKpis(items) {
    const wrap = document.getElementById("kpis");
    if (!wrap) return;
    wrap.innerHTML = "";
    for (const k of items || []) {
      const art = document.createElement("article");
      art.className = "card";
      art.innerHTML =
        '<section class="card-body"><p class="kpi-title mb-0">' +
        (k.title || k.key) +
        '</p><p class="kpi-value mb-0">' +
        (k.value || "—") +
        "</p>" +
        (k.hint ? '<p class="admin-muted small mb-0">' + k.hint + "</p>" : "") +
        "</section>";
      wrap.appendChild(art);
    }
  }

  function upsertLineChart(id, labels, datasets) {
    if (charts[id]) charts[id].destroy();
    const ctx = document.getElementById(id);
    if (!ctx) return;
    charts[id] = new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: { responsive: true, maintainAspectRatio: false },
    });
  }

  function upsertPieChart(id, labels, data) {
    if (charts[id]) charts[id].destroy();
    const ctx = document.getElementById(id);
    if (!ctx) return;
    charts[id] = new Chart(ctx, {
      type: "doughnut",
      data: { labels, datasets: [{ data }] },
      options: { responsive: true, maintainAspectRatio: false },
    });
  }

  function upsertBarChart(id, labels, datasets) {
    if (charts[id]) charts[id].destroy();
    const ctx = document.getElementById(id);
    if (!ctx) return;
    charts[id] = new Chart(ctx, {
      type: "bar",
      data: { labels, datasets },
      options: { responsive: true, maintainAspectRatio: false },
    });
  }

  function renderStaff(rows) {
    const body = document.getElementById("staffRows");
    if (!body) return;
    const items = rows || [];
    if (!items.length) {
      body.innerHTML = '<tr><td colspan="9" class="admin-muted">无数据</td></tr>';
      return;
    }
    body.innerHTML = items
      .map(function (r) {
        const goodRate = r.good_rate != null ? pct(r.good_rate) : "—";
        const adoptRate = r.adopt_rate != null ? pct(r.adopt_rate) : "—";
        return (
          "<tr>" +
          "<td>" +
          (r.name || r.username || "user#" + r.user_id) +
          "</td>" +
          "<td class='text-end'>" +
          fmtInt(r.total_msgs || 0) +
          "</td>" +
          "<td class='text-end'>" +
          fmtInt(r.ai_replies || 0) +
          "</td>" +
          "<td class='text-end'>" +
          fmtInt(r.good || 0) +
          "</td>" +
          "<td class='text-end'>" +
          fmtInt(r.bad || 0) +
          "</td>" +
          "<td class='text-end'><span class='badge bg-secondary-lt'>" +
          goodRate +
          "</span></td>" +
          "<td class='text-end'>" +
          fmtInt(r.adopted || 0) +
          "</td>" +
          "<td class='text-end'><span class='badge bg-secondary-lt'>" +
          adoptRate +
          "</span></td>" +
          "<td class='text-end'>" +
          fmtInt(r.outbound || 0) +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function render(d) {
    renderKpis(d.kpis);
    const ct = d.chat_trend || { labels: [], total: [], assistant: [] };
    upsertLineChart("chatTrend", ct.labels || [], [
      { label: "消息数", data: ct.total || [], borderWidth: 2, tension: 0.25 },
      { label: "AI 回复", data: ct.assistant || [], borderWidth: 2, tension: 0.25 },
    ]);
    const ot = d.outbound_trend || {
      labels: [],
      total: [],
      sent: [],
      failed: [],
      blocked: [],
    };
    upsertLineChart("outTrend", ot.labels || [], [
      { label: "总外发", data: ot.total || [], borderWidth: 2, tension: 0.25 },
      { label: "成功", data: ot.sent || [], borderWidth: 2, tension: 0.25 },
      { label: "失败", data: ot.failed || [], borderWidth: 2, tension: 0.25 },
      { label: "拦截", data: ot.blocked || [], borderWidth: 2, tension: 0.25 },
    ]);
    const oa = d.outbound_action || { direct_send: 0, edit_send: 0 };
    upsertPieChart("outboundTypePie", ["直发(send)", "编辑后发送(edit_send)"], [
      oa.direct_send || 0,
      oa.edit_send || 0,
    ]);
    const rp = d.rating || { good: 0, bad: 0, none: 0 };
    upsertPieChart("ratingPie", ["👍 好评", "👎 差评", "未评"], [
      rp.good || 0,
      rp.bad || 0,
      rp.none || 0,
    ]);
    const mb = d.model_stats || { labels: [], good_rate: [], adopt_rate: [] };
    upsertBarChart("modelBar", mb.labels || [], [
      {
        label: "好评率",
        data: (mb.good_rate || []).map(function (x) {
          return (x * 100).toFixed(1);
        }),
      },
      {
        label: "采纳率",
        data: (mb.adopt_rate || []).map(function (x) {
          return (x * 100).toFixed(1);
        }),
      },
    ]);
    renderStaff(d.staff || []);
  }

  let wired = false;

  function boot() {
    if (!document.getElementById("kpis")) return;
    const btnRefresh = document.getElementById("btn-refresh");
    const daysEl = document.getElementById("days");
    if (!wired) {
      wired = true;
      if (btnRefresh) btnRefresh.addEventListener("click", load);
      if (daysEl) daysEl.addEventListener("change", load);
      setInterval(load, 60 * 1000);
    }
    load();
  }

  boot();
  document.addEventListener("admin-panel-loaded", boot);
})();
