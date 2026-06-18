(function () {
  "use strict";

  let charts = {};

  const INCREMENTAL_PLACEHOLDERS = [
    {
      key: "incremental_updated",
      title: "今日活跃对",
      value: "…",
      hint: "销售号已绑定且今日有聊天",
    },
    {
      key: "incremental_pending",
      title: "今晚待画像",
      value: "…",
      hint: "销售号已绑定且今日有聊天、待重画/首画",
    },
    {
      key: "incremental_completed_24h",
      title: "24h已画像",
      value: "…",
      hint: "最近 24h 内成功画像条数",
    },
  ];

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

  function mergeKpisWithIncremental(mainKpis, incrementalKpis) {
    const out = (mainKpis || []).slice();
    const insertAfter = out.findIndex(function (k) {
      return k.key === "outbound_breakdown";
    });
    const idx = insertAfter >= 0 ? insertAfter + 1 : out.length;
    out.splice(idx, 0, ...(incrementalKpis || INCREMENTAL_PLACEHOLDERS));
    return out;
  }

  function incrementalUrl() {
    const u = new URL(window.location.href);
    const path = u.pathname.replace(/\/$/, "");
    const base = path.endsWith("/dashboard") ? path : path + "/dashboard";
    return base + "/incremental";
  }

  async function loadIncremental() {
    try {
      const r = await fetch(incrementalUrl(), { credentials: "same-origin" });
      if (!r.ok) return;
      const data = await r.json();
      patchIncrementalKpis(data.kpis || []);
    } catch (err) {
      console.warn("夜间增量 KPI 加载失败", err);
    }
  }

  function patchIncrementalKpis(items) {
    const wrap = document.getElementById("kpis");
    if (!wrap) return;
    for (const k of items) {
      const card = wrap.querySelector('[data-kpi-key="' + k.key + '"]');
      if (!card) continue;
      const valueEl = card.querySelector(".kpi-value");
      if (valueEl) valueEl.textContent = k.value || "—";
    }
  }

  async function load() {
    const days = document.getElementById("days").value || "7";
    const u = new URL(window.location.href);
    u.searchParams.set("format", "json");
    u.searchParams.set("days", days);
    const r = await fetch(u.toString(), { credentials: "same-origin" });
    const data = await r.json();
    render(
      Object.assign({}, data, {
        kpis: mergeKpisWithIncremental(data.kpis, INCREMENTAL_PLACEHOLDERS),
      })
    );
    setLastUpdated();
    loadIncremental();
  }

  function renderKpis(items) {
    const wrap = document.getElementById("kpis");
    if (!wrap) return;
    wrap.innerHTML = "";
    for (const k of items || []) {
      const art = document.createElement("article");
      art.className = "card";
      if (k.key) art.dataset.kpiKey = k.key;
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
    if (!ctx || typeof Chart === "undefined") return;
    if (!labels || !labels.length || !datasets || !datasets.length) return;
    charts[id] = new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: { responsive: true, maintainAspectRatio: false },
    });
  }

  function upsertPieChart(id, labels, data) {
    if (charts[id]) charts[id].destroy();
    const ctx = document.getElementById(id);
    if (!ctx || typeof Chart === "undefined") return;
    const safeLabels = labels && labels.length ? labels : ["暂无数据"];
    const safeData =
      data && data.length
        ? data
        : safeLabels.map(function () {
            return 1;
          });
    charts[id] = new Chart(ctx, {
      type: "doughnut",
      data: { labels: safeLabels, datasets: [{ data: safeData }] },
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

  function fmtTokens(n) {
    n = Number(n) || 0;
    if (n >= 1000000) return (n / 1000000).toFixed(2) + "M";
    if (n >= 10000) return (n / 1000).toFixed(1) + "K";
    return fmtInt(n);
  }

  function renderLlmUsage(lu) {
    lu = lu || {};
    const lt = lu.trend || {
      labels: [],
      prompt_tokens: [],
      completion_tokens: [],
      total_tokens: [],
      call_count: [],
    };
    if (!lu.available) {
      upsertPieChart("llmScenarioPie", ["未就绪"], [1]);
      renderLlmScenario([], false);
      return;
    }
    const labels = lt.labels || [];
    const totalTokens = lt.total_tokens || [];
    const callCounts = lt.call_count || [];
    const hasTokenTrend = totalTokens.some(function (n) {
      return Number(n) > 0;
    });
    const hasCallTrend = callCounts.some(function (n) {
      return Number(n) > 0;
    });
    if (hasTokenTrend) {
      upsertLineChart("llmTokenTrend", labels, [
        {
          label: "Prompt",
          data: lt.prompt_tokens || [],
          borderWidth: 2,
          tension: 0.25,
        },
        {
          label: "Completion",
          data: lt.completion_tokens || [],
          borderWidth: 2,
          tension: 0.25,
        },
        {
          label: "Total",
          data: totalTokens,
          borderWidth: 2,
          tension: 0.25,
        },
      ]);
    } else if (hasCallTrend) {
      upsertLineChart("llmTokenTrend", labels, [
        {
          label: "调用次数",
          data: callCounts,
          borderWidth: 2,
          tension: 0.25,
        },
      ]);
    } else if (labels.length) {
      upsertLineChart("llmTokenTrend", labels, [
        {
          label: "Total",
          data: labels.map(function () {
            return 0;
          }),
          borderWidth: 2,
          tension: 0.25,
        },
      ]);
    }
    const scenarios = lu.by_scenario || [];
    if (scenarios.length) {
      const tokenData = scenarios.map(function (s) {
        return Number(s.total_tokens) || 0;
      });
      const useCalls = tokenData.every(function (n) {
        return n <= 0;
      });
      upsertPieChart(
        "llmScenarioPie",
        scenarios.map(function (s) {
          return s.scenario_label || s.scenario_key;
        }),
        useCalls
          ? scenarios.map(function (s) {
              return Number(s.call_count) || 0;
            })
          : tokenData
      );
    } else {
      upsertPieChart("llmScenarioPie", ["暂无调用"], [1]);
    }
    renderLlmScenario(scenarios, true);
  }

  function renderLlmScenario(rows, available) {
    const body = document.getElementById("llmScenarioRows");
    if (!body) return;
    if (!available) {
      body.innerHTML =
        '<tr><td colspan="7" class="admin-muted">LLM 用量表未就绪，请执行数据库迁移</td></tr>';
      return;
    }
    const items = rows || [];
    if (!items.length) {
      body.innerHTML = '<tr><td colspan="7" class="admin-muted">窗口内暂无 LLM 调用记录</td></tr>';
      return;
    }
    body.innerHTML = items
      .map(function (r) {
        return (
          "<tr>" +
          "<td><span class='admin-muted small'>" +
          (r.scenario_key || "") +
          "</span><br/>" +
          (r.scenario_label || r.scenario_key || "—") +
          "</td>" +
          "<td class='text-end'>" +
          fmtInt(r.call_count || 0) +
          "</td>" +
          "<td class='text-end'>" +
          fmtTokens(r.prompt_tokens || 0) +
          "</td>" +
          "<td class='text-end'>" +
          fmtTokens(r.completion_tokens || 0) +
          "</td>" +
          "<td class='text-end'><strong>" +
          fmtTokens(r.total_tokens || 0) +
          "</strong></td>" +
          "<td class='text-end'>" +
          fmtInt(r.avg_duration_ms || 0) +
          " ms</td>" +
          "<td class='text-end'>" +
          fmtInt(r.fallback_count || 0) +
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
    try {
      renderLlmUsage(d.llm_usage || {});
    } catch (err) {
      console.error("LLM 用量渲染失败", err);
      renderLlmScenario([], false);
    }
    renderStaff(d.staff || []);
  }

  let wired = false;

  function boot() {
    if (!document.getElementById("kpis")) return;
    if (typeof Chart === "undefined") {
      window.setTimeout(boot, 120);
      return;
    }
    const btnRefresh = document.getElementById("btn-refresh");
    const daysEl = document.getElementById("days");
    if (!wired) {
      wired = true;
      if (btnRefresh) btnRefresh.addEventListener("click", load);
      if (daysEl) daysEl.addEventListener("change", load);
      setInterval(load, 5 * 60 * 1000);
    }
    load();
  }

  boot();
  document.addEventListener("admin-panel-loaded", boot);
})();
