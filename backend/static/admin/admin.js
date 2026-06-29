/* 管理后台：工具页侧栏内加载（无整页跳转） */
(function () {
  "use strict";

  var ADMIN_PANEL_PATHS = [
    "/admin/dashboard",
    "/admin/task-monitor",
    "/admin/profiling-progress",
    "/admin/task-allocation",
    "/admin/profile-nightly",
    "/admin/sales-wechat-accounts/import-xlsx",
    "/admin/raw-customer-wechat-sync",
    "/admin/raw-chat-wechat-sync",
  ];

  function normalizePath(href) {
    try {
      var u = new URL(href, window.location.origin);
      return u.pathname.replace(/\/$/, "") || "/";
    } catch (e) {
      return "";
    }
  }

  function isPanelPath(path) {
    return ADMIN_PANEL_PATHS.indexOf(path) >= 0;
  }

  function runScripts(container) {
    if (!container) return;
    container.querySelectorAll("script").forEach(function (old) {
      var s = document.createElement("script");
      if (old.src) {
        s.src = old.src;
        s.async = false;
      } else {
        s.textContent = old.textContent;
      }
      document.body.appendChild(s);
    });
  }

  function scriptSrcKey(src) {
    return (src || "").split("?")[0];
  }

  function loadPageScripts(doc) {
    if (!doc) return Promise.resolve();
    var pending = [];
    doc.querySelectorAll("script[src]").forEach(function (old) {
      var src = old.getAttribute("src");
      if (!src) return;
      var key = scriptSrcKey(src);
      if (
        key.indexOf("/admin-static/pages/") < 0 &&
        key.indexOf("chart.js") < 0
      ) {
        return;
      }
      var exists = false;
      document.querySelectorAll("script[src]").forEach(function (node) {
        if (scriptSrcKey(node.getAttribute("src")) === key) exists = true;
      });
      if (exists) return;
      pending.push(
        new Promise(function (resolve, reject) {
          var s = document.createElement("script");
          s.src = src;
          s.async = false;
          s.onload = function () {
            resolve();
          };
          s.onerror = function () {
            reject(new Error("failed to load " + src));
          };
          document.body.appendChild(s);
        })
      );
    });
    return Promise.all(pending);
  }

  function setActiveNav(url) {
    var path = normalizePath(url);
    document.querySelectorAll("#navbar-menu a.nav-link").forEach(function (a) {
      var hp = normalizePath(a.getAttribute("href") || "");
      a.classList.toggle("active", hp === path);
    });
  }

  async function loadPanel(url, pushState) {
    var target = document.querySelector(".page-body .container-fluid > .row");
    if (!target) {
      window.location.href = url;
      return;
    }
    try {
      var res = await fetch(url, {
        credentials: "same-origin",
        headers: { Accept: "text/html" },
      });
      if (!res.ok) {
        window.location.href = url;
        return;
      }
      var html = await res.text();
      var doc = new DOMParser().parseFromString(html, "text/html");
      var row = doc.querySelector(".page-body .container-fluid > .row");
      if (!row) {
        window.location.href = url;
        return;
      }
      target.innerHTML = row.innerHTML;
      runScripts(target);
      await loadPageScripts(doc);

      var newTitle = doc.querySelector(".page-header .page-title");
      var newSub = doc.querySelector(".page-header .page-pretitle");
      var titleEl = document.querySelector(".page-header .page-title");
      var subEl = document.querySelector(".page-header .page-pretitle");
      if (newTitle && titleEl) titleEl.textContent = newTitle.textContent;
      if (subEl) {
        if (newSub) subEl.textContent = newSub.textContent;
        else subEl.textContent = "";
      }

      setActiveNav(url);
      if (pushState !== false) {
        history.pushState({ adminPanel: url }, "", url);
      }
      window.scrollTo(0, 0);
      document.dispatchEvent(
        new CustomEvent("admin-panel-loaded", { detail: { url: url } })
      );
    } catch (err) {
      window.location.href = url;
    }
  }

  document.addEventListener("click", function (e) {
    var a = e.target.closest("#navbar-menu a.nav-link[href]");
    if (!a || a.getAttribute("target") === "_blank") return;
    var path = normalizePath(a.getAttribute("href"));
    if (!isPanelPath(path)) return;
    e.preventDefault();
    loadPanel(a.href);
  });

  window.addEventListener("popstate", function () {
    if (history.state && history.state.adminPanel) {
      loadPanel(history.state.adminPanel, false);
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    if (history.state && history.state.adminPanel) return;
    if (isPanelPath(normalizePath(window.location.pathname))) {
      history.replaceState({ adminPanel: window.location.href }, "", window.location.href);
    }
    document.querySelectorAll("[data-bs-toggle='tooltip']").forEach(function (el) {
      if (typeof bootstrap !== "undefined" && bootstrap.Tooltip) {
        new bootstrap.Tooltip(el);
      }
    });
  });
})();
