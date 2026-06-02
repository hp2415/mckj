(function () {
  "use strict";

  function qs(obj) {
    const u = new URLSearchParams();
    for (const k in obj) {
      if (obj[k] !== "" && obj[k] != null) u.set(k, obj[k]);
    }
    return u.toString();
  }

  function readForm() {
    return {
      day: document.getElementById("day").value || "",
      sales_wechat_id: document.getElementById("sw").value.trim(),
      force: document.getElementById("force").checked ? "1" : "",
    };
  }

  function renderKpis(d) {
    const el = document.getElementById("kpis");
    if (!el) return;
    el.innerHTML = "";
    const items = [
      { k: "候选客户对", v: d.summary.total_pairs },
      { k: "窗口内聊天总条数", v: d.summary.total_chats },
      { k: "涉及销售号", v: d.summary.by_sales.length },
      {
        k: "窗口",
        v: d.window.day + (d.window.respect_watermark ? "" : " · 强制"),
      },
    ];
    for (const it of items) {
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML =
        '<div class="card-body py-3"><p class="kpi-title mb-0">' +
        it.k +
        '</p><p class="kpi-value mb-0">' +
        it.v +
        "</p></div>";
      el.appendChild(card);
    }
  }

  function renderBySales(d) {
    const tb = document.querySelector("#bySalesTable tbody");
    if (!tb) return;
    tb.innerHTML = "";
    for (const r of d.summary.by_sales) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td><code>" +
        r.sales_label +
        '</code><div class="admin-muted small">' +
        r.sales_wechat_id +
        "</div></td><td>" +
        (r.staff_name || '<span class="admin-muted">未绑定</span>') +
        "</td><td>" +
        r.pair_count +
        "</td><td>" +
        r.total_chats +
        "</td>";
      tb.appendChild(tr);
    }
  }

  function renderRows(d) {
    const tb = document.querySelector("#rowsTable tbody");
    if (!tb) return;
    tb.innerHTML = "";
    for (const r of d.rows) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" +
        r.customer_name +
        '<div class="admin-muted small">' +
        r.raw_customer_id +
        "</div></td><td><code>" +
        r.sales_label +
        "</code></td><td>" +
        (r.staff_name || '<span class="admin-muted">未绑定</span>') +
        "</td><td>" +
        r.latest_chat_at +
        "</td><td>" +
        r.chat_count +
        "</td><td>" +
        (r.profiled_at || "") +
        "</td>";
      tb.appendChild(tr);
    }
    if (d.rows_truncated) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        '<td colspan="6" class="admin-muted">… 候选超过 500 条，仅展示前 500 条，入队会处理全部 ' +
        d.summary.total_pairs +
        " 条。</td>";
      tb.appendChild(tr);
    }
  }

  async function refresh() {
    const btn = document.getElementById("btn-refresh");
    const hint = document.getElementById("hint");
    if (btn) btn.disabled = true;
    if (hint) hint.textContent = "正在加载候选…";
    try {
      const params = readForm();
      params.format = "json";
      const r = await fetch("/admin/profile-nightly?" + qs(params));
      const data = await r.json();
      if (!r.ok || data.ok === false) {
        throw new Error(data.message || "HTTP " + r.status);
      }
      renderKpis(data);
      renderBySales(data);
      renderRows(data);
      if (hint) {
        hint.textContent =
          "默认 = 今日 00:00 至当前；选历史日期则为该日全天 · 共 " +
          (data.summary?.total_pairs ?? 0) +
          " 对";
      }
    } catch (e) {
      console.error(e);
      alert("刷新失败：" + (e && e.message ? e.message : e));
      if (hint) hint.textContent = "加载失败，请重试";
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function doEnqueue() {
    if (!confirm("确认按当前过滤条件，把候选全部入队 profile_jobs？")) return;
    const btn = document.getElementById("btn-enqueue");
    if (btn) btn.disabled = true;
    try {
      const params = readForm();
      params.action = "enqueue";
      const r = await fetch("/admin/profile-nightly?" + qs(params), {
        method: "POST",
      });
      const data = await r.json();
      alert(
        "已入队 " +
          data.enqueued +
          " 对\n" +
          (data.label || data.message || "")
      );
    } catch (e) {
      console.error(e);
      alert("入队失败，请查看控制台");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  let wired = false;

  function boot() {
    if (!document.getElementById("kpis")) return;
    const dayEl = document.getElementById("day");
    if (dayEl && !dayEl.value) {
      dayEl.value = new Date().toLocaleDateString("sv-SE", {
        timeZone: "Asia/Shanghai",
      });
    }
    if (!wired) {
      wired = true;
      document
        .getElementById("btn-refresh")
        ?.addEventListener("click", refresh);
      document
        .getElementById("btn-enqueue")
        ?.addEventListener("click", doEnqueue);
    }
    refresh();
  }

  boot();
  document.addEventListener("admin-panel-loaded", boot);
})();
