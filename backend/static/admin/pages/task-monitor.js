(function () {
  "use strict";

  const BATCH_STATUS_LABELS = {
    draft: "草稿",
    published: "已发布",
    archived: "已归档",
    generating: "生成中",
    failed: "失败",
  };

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtInt(n) {
    try {
      return new Intl.NumberFormat().format(n);
    } catch (e) {
      return String(n);
    }
  }

  function pct(rate) {
    if (rate == null || isNaN(rate)) return "—";
    return Math.round(rate * 100) + "%";
  }

  function setLastUpdated() {
    const el = document.getElementById("last-updated");
    if (!el) return;
    el.textContent = "最近更新：" + new Date().toLocaleString();
  }

  function isHistoryMode() {
    const chk = document.getElementById("chk-history");
    return !!(chk && chk.checked);
  }

  function syncHistoryUi() {
    const show = isHistoryMode();
    const wrap = document.getElementById("historyDateWrap");
    const batchWrap = document.getElementById("batchStatusWrap");
    const period = (document.getElementById("period") || {}).value || "daily";
    if (wrap) wrap.style.display = show ? "" : "none";
    if (batchWrap) batchWrap.style.display = period === "monthly" ? "none" : "";
    if (!show) {
      const ref = document.getElementById("refDate");
      if (ref) ref.value = "";
    }
  }

  function buildOverviewUrl(salesWechatId) {
    const period = (document.getElementById("period") || {}).value || "daily";
    const params = new URLSearchParams();
    params.set("sales_wechat_id", salesWechatId);
    params.set("period", period);
    if (isHistoryMode()) {
      const ref = document.getElementById("refDate");
      if (ref && ref.value) {
        params.set("date", ref.value);
      }
    }
    const bs = document.getElementById("batchStatus");
    if (bs && bs.value && period !== "monthly") {
      params.set("batch_status", bs.value);
    }
    return "/admin/task-allocation?" + params.toString();
  }

  function progressClass(rate) {
    const p = Math.round((rate || 0) * 100);
    if (p < 30) return "critical";
    if (p < 60) return "low";
    return "";
  }

  function renderSummary(summary, meta) {
    const s = summary || {};
    document.getElementById("s-sales").textContent = fmtInt(s.sales_count || 0);
    document.getElementById("s-total").textContent = fmtInt(s.total || 0);
    document.getElementById("s-done").textContent = fmtInt(s.done || 0);
    document.getElementById("s-pending").textContent = fmtInt(
      (s.pending || 0) + (s.in_progress || 0)
    );
    document.getElementById("s-overdue").textContent = fmtInt(s.overdue || 0);
    document.getElementById("s-rate").textContent = pct(s.completion_rate);

    const metaEl = document.getElementById("metaLine");
    if (!metaEl) return;
    let text =
      "周期 <strong>" +
      escapeHtml(meta.period_start) +
      "</strong> ~ <strong>" +
      escapeHtml(meta.period_end) +
      "</strong>";
    if (meta.is_historical) {
      text += ' · <span class="badge tm-badge-historical">历史查看</span>';
      if (meta.ref_date) {
        text += " · 参考日 <strong>" + escapeHtml(meta.ref_date) + "</strong>";
      }
    }
    if (meta.period_type === "monthly") {
      text += " · 月进度汇总（按截止日）";
    }
    metaEl.innerHTML = text;
  }

  function renderRows(items) {
    const body = document.getElementById("rows");
    if (!body) return;
    const rows = items || [];
    if (!rows.length) {
      body.innerHTML =
        '<tr><td colspan="10" class="admin-muted text-center py-4">当前条件下暂无任务数据</td></tr>';
      return;
    }

    body.innerHTML = rows
      .map(function (r) {
        const st = r.stats || {};
        const rate = st.completion_rate || 0;
        const pCls = progressClass(rate);
        const sw = r.sales_wechat_id || "";
        const label = r.label || sw;
        const nickname = r.nickname && r.nickname !== sw ? r.nickname : "";
        const pending = (st.pending || 0) + (st.in_progress || 0);
        let batchCell = "—";
        if (r.view_mode === "generating") {
          batchCell =
            '<span class="badge tm-badge-generating">生成中</span>';
        } else if (r.view_mode === "month_progress") {
          batchCell = '<span class="badge bg-secondary-lt">月汇总</span>';
        } else if (r.batch_id) {
          const stLab =
            BATCH_STATUS_LABELS[r.batch_status] || r.batch_status || "";
          batchCell =
            "#" +
            escapeHtml(r.batch_id) +
            ' <span class="badge bg-secondary-lt">' +
            escapeHtml(stLab) +
            "</span>";
        }
        const url = buildOverviewUrl(sw);
        return (
          '<tr class="tm-row-clickable" data-href="' +
          escapeHtml(url) +
          '">' +
          '<td class="tm-sales-cell"><div>' +
          escapeHtml(label) +
          "</div>" +
          (nickname
            ? '<div class="sub">' + escapeHtml(sw) + "</div>"
            : "") +
          "</td>" +
          '<td class="text-end">' +
          fmtInt(st.total || 0) +
          "</td>" +
          '<td class="text-end">' +
          fmtInt(r.main_wechat || 0) +
          "</td>" +
          '<td class="text-end">' +
          fmtInt(r.main_phone || 0) +
          "</td>" +
          '<td class="text-end">' +
          fmtInt(r.ice || 0) +
          "</td>" +
          '<td class="text-end">' +
          fmtInt(pending) +
          "</td>" +
          '<td class="text-end">' +
          fmtInt(st.overdue || 0) +
          "</td>" +
          '<td class="text-end tm-progress">' +
          pct(rate) +
          '<div class="tm-progress-track"><div class="tm-progress-fill ' +
          pCls +
          '" style="width:' +
          Math.round(rate * 100) +
          '%"></div></div></td>' +
          "<td>" +
          batchCell +
          "</td>" +
          '<td><a class="btn btn-sm btn-outline-primary" href="' +
          escapeHtml(url) +
          '" onclick="event.stopPropagation()">详情</a></td>' +
          "</tr>"
        );
      })
      .join("");

    body.querySelectorAll("tr.tm-row-clickable").forEach(function (tr) {
      tr.addEventListener("click", function () {
        const href = tr.getAttribute("data-href");
        if (href) window.location.href = href;
      });
    });
  }

  async function load() {
    const period = (document.getElementById("period") || {}).value || "daily";
    if (isHistoryMode()) {
      const ref = document.getElementById("refDate");
      if (!ref || !ref.value) {
        document.getElementById("rows").innerHTML =
          '<tr><td colspan="10" class="admin-muted text-center py-4">请选择参考日期以查看历史数据</td></tr>';
        document.getElementById("metaLine").textContent =
          "历史模式下需选择参考日期（按该日期所在日/周/月定位周期）";
        return;
      }
    }
    const params = new URLSearchParams();
    params.set("format", "json");
    params.set("period", period);
    if (isHistoryMode()) {
      const ref = document.getElementById("refDate");
      if (ref && ref.value) params.set("date", ref.value);
    }
    const bs = document.getElementById("batchStatus");
    if (bs && bs.value && period !== "monthly") {
      params.set("batch_status", bs.value);
    }

    const u = new URL(window.location.href);
    u.search = params.toString();
    const r = await fetch(u.toString(), { credentials: "same-origin" });
    const data = await r.json();
    if (!data.ok) {
      document.getElementById("rows").innerHTML =
        '<tr><td colspan="10" class="text-danger text-center py-4">加载失败</td></tr>';
      return;
    }
    renderSummary(data.summary, data);
    renderRows(data.items);
    setLastUpdated();
  }

  let wired = false;

  function boot() {
    if (!document.getElementById("rows")) return;
    syncHistoryUi();

    if (!wired) {
      wired = true;
      const btn = document.getElementById("btn-refresh");
      const period = document.getElementById("period");
      const chk = document.getElementById("chk-history");
      const ref = document.getElementById("refDate");
      const bs = document.getElementById("batchStatus");
      if (btn) btn.addEventListener("click", load);
      if (period) {
        period.addEventListener("change", function () {
          syncHistoryUi();
          load();
        });
      }
      if (chk) {
        chk.addEventListener("change", function () {
          syncHistoryUi();
          load();
        });
      }
      if (ref) ref.addEventListener("change", load);
      if (bs) bs.addEventListener("change", load);
      setInterval(load, 5 * 60 * 1000);
    }
    load();
  }

  boot();
  document.addEventListener("admin-panel-loaded", boot);
})();
