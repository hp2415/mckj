(function () {
  "use strict";

  function fmt(ts) {
    if (ts == null) return "";
    const d = new Date(ts * 1000);
    return isNaN(d.getTime()) ? "" : d.toLocaleString();
  }

  function panelApiUrl() {
    const path = window.location.pathname.replace(/\/$/, "") || "/";
    if (path.endsWith("/profiling-progress")) {
      return path;
    }
    return "/admin/profiling-progress";
  }

  async function postAction(action, params) {
    const u = new URL(panelApiUrl(), window.location.origin);
    u.searchParams.set("action", action);
    if (params) {
      Object.keys(params).forEach(function (k) {
        u.searchParams.set(k, params[k]);
      });
    }
    const r = await fetch(u.toString(), {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    const ct = (r.headers.get("content-type") || "").toLowerCase();
    if (!r.ok) {
      const text = await r.text();
      throw new Error(text || "HTTP " + r.status);
    }
    if (!ct.includes("application/json")) {
      throw new Error("响应非 JSON（可能未登录或路由错误）");
    }
    return await r.json();
  }

  function showHint(message, preferConcurrency) {
    const hint = preferConcurrency
      ? document.getElementById("concurrency-hint")
      : document.getElementById("cancel-hint");
    const fallback = preferConcurrency
      ? document.getElementById("cancel-hint")
      : document.getElementById("concurrency-hint");
    const el = hint || fallback;
    if (el) el.textContent = message;
  }

  function bindBtn(id, action, paramsFn, preferConcurrencyHint) {
    const el = document.getElementById(id);
    if (!el) return;
    const boundKey = "profileAction:" + action;
    if (el.dataset.profileActionBound === boundKey) return;
    el.dataset.profileActionBound = boundKey;
    el.addEventListener("click", async function () {
      const btn = this;
      btn.disabled = true;
      try {
        const j = await postAction(action, paramsFn ? paramsFn() : null);
        showHint(
          j && j.message ? j.message : "已提交",
          !!preferConcurrencyHint
        );
      } catch (e) {
        showHint(
          "操作失败：" + (e && e.message ? e.message : "请确认已登录后台"),
          !!preferConcurrencyHint
        );
      }
      setTimeout(function () {
        btn.disabled = false;
      }, 1200);
    });
  }

  function wireActionButtons() {
    bindBtn("btn-pause", "pause");
    bindBtn("btn-resume", "resume");
    bindBtn("btn-cancel", "cancel");
    bindBtn("btn-clear-cancel", "clear_cancel");
    bindBtn("btn-cancel-all", "cancel_all_pending");
    bindBtn("btn-reclaim-stale", "reclaim_stale", function () {
      return { stale_minutes: 30 };
    });
    bindBtn(
      "btn-save-concurrency",
      "set_concurrency",
      function () {
        const el = document.getElementById("worker-concurrency");
        return { concurrency: el ? el.value : "4" };
      },
      true
    );
  }

  async function tick() {
    try {
      const u = new URL(panelApiUrl(), window.location.origin);
      u.searchParams.set("format", "json");
      const r = await fetch(u.toString(), { credentials: "same-origin" });
      const d = await r.json();
      const st = {
        idle: "空闲",
        running: "运行中",
        paused: "已暂停",
        completed: "已完成",
        failed: "失败",
        cancelled: "已中断",
      };
      const statusEl = document.getElementById("status");
      if (statusEl) statusEl.textContent = st[d.status] || d.status;
      const llmEl = document.getElementById("llm-info");
      if (llmEl) {
        const pl = d.profile_llm || {};
        llmEl.textContent = pl.model
          ? pl.model + (pl.api_host ? " · API: " + pl.api_host : "")
          : "—";
      }
      const cur = d.current_batch || {};
      const cbs = cur.counts_by_status || {};
      const total = cur.total || 0;
      const done = cbs.succeeded || cur.done || 0;
      const running = cbs.running || cur.running || 0;
      const pending = cbs.pending || cur.pending || 0;
      const failed = cbs.failed || cur.failed || 0;
      const cancelled = cbs.cancelled || cur.cancelled || 0;
      const chip = document.getElementById("running-chip");
      if (chip) chip.textContent = "running=" + running;
      const concEl = document.getElementById("worker-concurrency");
      if (concEl && d.worker_concurrency != null && document.activeElement !== concEl) {
        concEl.value = String(d.worker_concurrency);
        const mx = d.worker_concurrency_max || 32;
        concEl.max = String(mx);
      }
      let binfo = "—";
      if (cur.batch_label || cur.batch_id) {
        binfo = (cur.batch_label || "") + (cur.batch_id ? " · id=" + cur.batch_id : "");
      }
      const countsEl = document.getElementById("counts");
      if (countsEl) {
        countsEl.textContent = total
          ? "本次 " +
            binfo +
            " · 总 " +
            total +
            "（成功 " +
            done +
            "，运行中 " +
            running +
            "，排队 " +
            pending +
            "，失败 " +
            failed +
            "，取消 " +
            cancelled +
            "）"
          : "—";
      }
      function pct(x) {
        return total ? (100.0 * x) / total : 0;
      }
      const segDone = document.getElementById("seg-done");
      const segRunning = document.getElementById("seg-running");
      const segPending = document.getElementById("seg-pending");
      const segFailed = document.getElementById("seg-failed");
      const segCancelled = document.getElementById("seg-cancelled");
      if (segDone) segDone.style.width = pct(done) + "%";
      if (segRunning) segRunning.style.width = pct(running) + "%";
      if (segPending) segPending.style.width = pct(pending) + "%";
      if (segFailed) segFailed.style.width = pct(failed) + "%";
      if (segCancelled) segCancelled.style.width = pct(cancelled) + "%";
      let extra = "";
      if (pending > 0) extra += "排队任务：" + pending;
      const flags = [];
      if (d.paused) flags.push("已暂停");
      if (d.cancel_requested) flags.push("已中断(停止抢任务)");
      const msgEl = document.getElementById("msg");
      if (msgEl) {
        msgEl.textContent =
          (flags.length ? flags.join(" · ") : "") + (extra ? " · " + extra : "");
      }
      const rj = d.running_jobs || [];
      const rw = document.getElementById("running-wrap");
      if (rw) {
        if (!rj.length) {
          rw.innerHTML = '<p class="admin-muted">无</p>';
        } else {
          const rows = rj
            .map(function (p, i) {
              return (
                "<tr><td>" +
                (i + 1) +
                "</td><td><code>" +
                (p.target || "") +
                "</code></td><td>" +
                (p.locked_by || "") +
                "</td><td>" +
                (p.locked_at || "") +
                "</td></tr>"
              );
            })
            .join("");
          rw.innerHTML =
            "<table class='table table-sm'><thead><tr><th>#</th><th>任务</th><th>worker</th><th>锁定时间</th></tr></thead><tbody>" +
            rows +
            "</tbody></table>";
        }
      }
      const pend = d.pending_batches || [];
      const pw = document.getElementById("pending-wrap");
      if (pw) {
        if (!pend.length) {
          pw.innerHTML = '<p class="admin-muted">无</p>';
        } else {
          const rows = pend
            .map(function (p, i) {
              const bid = p.batch_id || "";
              const btn = bid
                ? '<button class="btn btn-warning btn-sm" data-batch="' +
                  bid +
                  '">取消该批次排队</button>'
                : "";
              return (
                "<tr><td>" +
                (i + 1) +
                "</td><td>" +
                (p.label || "") +
                "</td><td>" +
                (p.count != null ? p.count : "—") +
                "</td><td>" +
                fmt(p.enqueued_at) +
                "</td><td><code>" +
                bid +
                "</code></td><td>" +
                btn +
                "</td></tr>"
              );
            })
            .join("");
          pw.innerHTML =
            "<table class='table table-sm'><thead><tr><th>#</th><th>说明</th><th>条数</th><th>入队时间</th><th>batch_id</th><th>操作</th></tr></thead><tbody>" +
            rows +
            "</tbody></table>";
          pw.querySelectorAll("button[data-batch]").forEach(function (btn) {
            btn.addEventListener("click", async function () {
              const batchId = this.getAttribute("data-batch");
              this.disabled = true;
              try {
                const j = await postAction("cancel_batch", { batch_id: batchId });
                const hint = document.getElementById("cancel-hint");
                if (hint) hint.textContent = j && j.message ? j.message : "已提交";
              } catch (e) {
                const hint = document.getElementById("cancel-hint");
                if (hint) hint.textContent = "取消批次失败";
              }
              setTimeout(function () {
                btn.disabled = false;
              }, 1200);
            });
          });
        }
      }
      const errs = d.recent_errors || [];
      const ep = document.getElementById("errors");
      if (ep) {
        if (!errs.length) {
          ep.textContent = "无";
        } else {
          ep.textContent = errs
            .map(function (e) {
              return (
                fmt(e.at) +
                "  [" +
                (e.target || "") +
                "]\n" +
                (e.message || "") +
                "\n---\n"
              );
            })
            .join("");
        }
      }
    } catch (e) {
      const msgEl = document.getElementById("msg");
      if (msgEl) msgEl.textContent = "无法拉取状态（请保持管理后台已登录）";
    }
  }

  let wired = false;

  function boot() {
    if (!document.getElementById("status")) return;
    wireActionButtons();
    if (!wired) {
      wired = true;
      setInterval(tick, 2000);
    }
    tick();
  }

  boot();
  document.addEventListener("admin-panel-loaded", boot);
})();
