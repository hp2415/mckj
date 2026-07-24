(function () {
  "use strict";

  const TASK_CATEGORY_LABELS = {
    all: "全部",
    main: "主线任务",
    icebreaker: "激活任务（前破冰）",
  };

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

  function getTaskCategory() {
    const el = document.getElementById("taskCategory");
    return (el && el.value) || "all";
  }

  function syncCategoryUi() {
    const cat = getTaskCategory();
    const table = document.querySelector(".tm-table");
    if (table) table.setAttribute("data-category", cat);
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

  function renderSalesCell(r) {
    const sw = r.sales_wechat_id || "";
    const staff = (r.staff_name || "").trim();
    const label = r.label || sw;
    const nickname = r.nickname && r.nickname !== sw ? r.nickname : "";
    let html = '<td class="tm-sales-cell">';
    if (staff) {
      html +=
        "<div>" +
        escapeHtml(staff) +
        ' <span class="tm-sales-sep">·</span> ' +
        '<span class="tm-sw">' +
        escapeHtml(sw) +
        "</span></div>";
      if (nickname) {
        html += '<div class="sub">' + escapeHtml(nickname) + "</div>";
      }
    } else {
      html += "<div>" + escapeHtml(label) + "</div>";
      if (nickname) {
        html += '<div class="sub">' + escapeHtml(sw) + "</div>";
      }
    }
    html += "</td>";
    return html;
  }

  function progressSegments(st) {
    const total = st.total || 0;
    const done = st.done || 0;
    const skipped = st.skipped || 0;
    if (!total) {
      return { donePct: 0, skipPct: 0 };
    }
    return {
      donePct: (done / total) * 100,
      skipPct: (skipped / total) * 100,
    };
  }

  function buildProgressBar(donePct, skipPct, pCls, large) {
    const doneW = Math.max(0, Math.min(100, donePct));
    const skipW = Math.max(0, Math.min(100, skipPct));
    const trackCls =
      "tm-progress-track" + (large ? " tm-progress-track-lg" : "");
    let html = '<div class="' + trackCls + '">';
    if (doneW > 0) {
      html +=
        '<span class="tm-progress-seg done ' +
        (pCls || "") +
        '" style="width:' +
        doneW +
        '%"></span>';
    }
    if (skipW > 0) {
      html +=
        '<span class="tm-progress-seg skip" style="width:' +
        skipW +
        '%"></span>';
    }
    html += "</div>";
    return html;
  }

  function renderProgressCell(st) {
    const skipped = st.skipped || 0;
    const rate = st.completion_rate || 0;
    const segs = progressSegments(st);
    const skipPct = Math.round(segs.skipPct);
    const pCls = progressClass(rate);
    let text = pct(rate);
    if (skipPct > 0) {
      text +=
        ' <span class="tm-skip-rate" title="跳过 ' +
        fmtInt(skipped) +
        ' 条">跳' +
        skipPct +
        "%</span>";
    }
    return (
      '<td class="text-end tm-progress">' +
      '<div class="tm-progress-label">' +
      text +
      "</div>" +
      buildProgressBar(segs.donePct, segs.skipPct, pCls, false) +
      "</td>"
    );
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
    const rateEl = document.getElementById("s-rate");
    const rateBarEl = document.getElementById("s-rate-bar");
    if (rateEl) {
      const segs = progressSegments(s);
      const skipPct = Math.round(segs.skipPct);
      let rateText = pct(s.completion_rate);
      if (skipPct > 0) {
        rateText += ' <span class="tm-skip-rate">跳' + skipPct + "%</span>";
      }
      rateEl.innerHTML = rateText;
      // if (rateBarEl) {
      //   rateBarEl.innerHTML = buildProgressBar(
      //     segs.donePct,
      //     segs.skipPct,
      //     progressClass(s.completion_rate || 0),
      //     true
      //   );
      // }
    }

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
    if (meta.task_category && meta.task_category !== "all") {
      text +=
        ' · 类别 <strong>' +
        escapeHtml(TASK_CATEGORY_LABELS[meta.task_category] || meta.task_category) +
        "</strong>";
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
        const sw = r.sales_wechat_id || "";
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
          renderSalesCell(r) +
          '<td class="text-end">' +
          fmtInt(st.total || 0) +
          "</td>" +
          '<td class="text-end tm-cell-main-wechat">' +
          fmtInt(r.main_wechat || 0) +
          "</td>" +
          '<td class="text-end tm-cell-main-phone">' +
          fmtInt(r.main_phone || 0) +
          "</td>" +
          '<td class="text-end tm-cell-ice">' +
          fmtInt(r.ice || 0) +
          "</td>" +
          '<td class="text-end">' +
          fmtInt(pending) +
          "</td>" +
          '<td class="text-end">' +
          fmtInt(st.overdue || 0) +
          "</td>" +
          renderProgressCell(st) +
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

  function toIsoDate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return y + "-" + m + "-" + day;
  }

  function initExportDates() {
    const fromEl = document.getElementById("exportDateFrom");
    const toEl = document.getElementById("exportDateTo");
    if (!fromEl || !toEl) return;
    if (fromEl.value && toEl.value) return;
    const now = new Date();
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);
    if (!fromEl.value) fromEl.value = toIsoDate(monthStart);
    if (!toEl.value) toEl.value = toIsoDate(now);
  }

  async function exportCsv() {
    const fromEl = document.getElementById("exportDateFrom");
    const toEl = document.getElementById("exportDateTo");
    const dateFrom = (fromEl && fromEl.value) || "";
    const dateTo = (toEl && toEl.value) || "";
    if (!dateFrom || !dateTo) {
      window.alert("请先选择导出的起止日期");
      return;
    }
    if (dateFrom > dateTo) {
      window.alert("开始日期不能晚于结束日期");
      return;
    }
    const params = new URLSearchParams();
    params.set("format", "csv");
    params.set("date_from", dateFrom);
    params.set("date_to", dateTo);
    const cat = getTaskCategory();
    if (cat && cat !== "all") {
      params.set("task_category", cat);
    }
    const u = new URL(window.location.href);
    u.search = params.toString();
    const btn = document.getElementById("btn-export");
    if (btn) btn.disabled = true;
    try {
      const r = await fetch(u.toString(), { credentials: "same-origin" });
      const ct = (r.headers.get("content-type") || "").toLowerCase();
      if (!r.ok || ct.indexOf("text/csv") < 0) {
        let msg = "导出失败";
        try {
          const err = await r.json();
          if (err && err.error) msg = err.error;
        } catch (e) {
          /* ignore */
        }
        window.alert(msg);
        return;
      }
      const blob = await r.blob();
      let filename =
        "task_monitor_" + dateFrom + "_" + dateTo + ".csv";
      const cd = r.headers.get("content-disposition") || "";
      const m = /filename=\"?([^\";]+)\"?/i.exec(cd);
      if (m && m[1]) filename = m[1];
      const a = document.createElement("a");
      const url = URL.createObjectURL(blob);
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      window.alert("导出失败，请稍后重试");
    } finally {
      if (btn) btn.disabled = false;
    }
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
    const cat = getTaskCategory();
    if (cat && cat !== "all") {
      params.set("task_category", cat);
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
    syncCategoryUi();
    renderRows(data.items);
    setLastUpdated();
  }

  let wired = false;

  function boot() {
    if (!document.getElementById("rows")) return;
    syncHistoryUi();
    syncCategoryUi();
    initExportDates();

    if (!wired) {
      wired = true;
      const btn = document.getElementById("btn-refresh");
      const btnExport = document.getElementById("btn-export");
      const period = document.getElementById("period");
      const taskCategory = document.getElementById("taskCategory");
      const chk = document.getElementById("chk-history");
      const ref = document.getElementById("refDate");
      const bs = document.getElementById("batchStatus");
      if (btn) btn.addEventListener("click", load);
      if (btnExport) btnExport.addEventListener("click", exportCsv);
      if (period) {
        period.addEventListener("change", function () {
          syncHistoryUi();
          load();
        });
      }
      if (taskCategory) {
        taskCategory.addEventListener("change", function () {
          syncCategoryUi();
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
