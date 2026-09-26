/* Udemy Enroller - front-end interactions (vanilla, no build step). */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── Sidebar: mobile drawer + desktop mini-collapse ──────────────────── */
  (function () {
    var hamburger = document.getElementById("hamburger");
    var scrim = document.getElementById("side-scrim");
    if (!hamburger) return;
    try {
      if (localStorage.getItem("ue-sidebar-mini") === "1" && window.innerWidth > 860) {
        document.body.classList.add("sidebar-mini");
      }
    } catch (e) {}
    hamburger.addEventListener("click", function () {
      if (window.innerWidth <= 860) {
        document.body.classList.toggle("sidebar-open");
      } else {
        var mini = document.body.classList.toggle("sidebar-mini");
        try { localStorage.setItem("ue-sidebar-mini", mini ? "1" : "0"); } catch (e) {}
      }
    });
    if (scrim) scrim.addEventListener("click", function () { document.body.classList.remove("sidebar-open"); });
  })();

  /* ── Theme toggle (works for any #theme-toggle on the page) ───────────── */
  function currentTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t) return t;
    return (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  }
  document.querySelectorAll("#theme-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("ue-theme", next); } catch (e) {}
    });
  });

  /* ── Toasts ──────────────────────────────────────────────────────────── */
  window.toast = function (msg, type) {
    var box = document.getElementById("toasts");
    if (!box) return;
    var el = document.createElement("div");
    el.className = "toast " + (type || "info");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity .3s, transform .3s";
      el.style.opacity = "0"; el.style.transform = "translateX(20px)";
      setTimeout(function () { el.remove(); }, 300);
    }, 3800);
  };

  /* ── Form POST helper ────────────────────────────────────────────────── */
  window.postForm = async function (url, data) {
    var res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "XMLHttpRequest" },
      body: new URLSearchParams(data)
    });
    var json = null; try { json = await res.json(); } catch (e) {}
    return { ok: res.ok, status: res.status, json: json };
  };

  /* ── Count-up ────────────────────────────────────────────────────────── */
  function countUp(el) {
    var target = parseFloat(el.getAttribute("data-count") || "0");
    var decimals = parseInt(el.getAttribute("data-decimals") || "0", 10);
    var prefix = el.getAttribute("data-prefix") || "";
    var suffix = el.getAttribute("data-suffix") || "";
    function fmt(v) { return prefix + v.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) + suffix; }
    if (reduceMotion) { el.textContent = fmt(target); return; }
    var dur = 900, start = performance.now();
    function frame(now) {
      var p = Math.min(1, (now - start) / dur);
      el.textContent = fmt(target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(frame); else el.textContent = fmt(target);
    }
    requestAnimationFrame(frame);
  }
  document.querySelectorAll("[data-count]").forEach(countUp);
  window.ueCountUp = countUp;

  /* ── Confetti ────────────────────────────────────────────────────────── */
  window.confettiBurst = function () {
    if (reduceMotion) return;
    var colors = ["#7C2FF0", "#6D28D9", "#0f9d63", "#c47f10", "#0b83c9"];
    var wrap = document.createElement("div"); wrap.className = "confetti";
    for (var i = 0; i < 80; i++) {
      var p = document.createElement("i");
      p.style.left = Math.random() * 100 + "vw";
      p.style.background = colors[i % colors.length];
      p.style.animationDelay = (Math.random() * 0.3) + "s";
      p.style.transform = "rotate(" + (Math.random() * 360) + "deg)";
      wrap.appendChild(p);
    }
    document.body.appendChild(wrap);
    setTimeout(function () { wrap.remove(); }, 2300);
  };

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* ── Enroll Now + auto runs: live progress, Stop, resume ─────────────── */
  var enrollBtn = document.getElementById("enroll-now-btn");
  var enrollPanel = document.getElementById("enroll-live");
  var ueResume = null;   // set below; the engine pill calls it when a run starts
  if (enrollPanel) {
    var stopBtn = document.getElementById("enroll-stop-btn");
    var csrf = (enrollBtn && enrollBtn.getAttribute("data-csrf")) || (stopBtn && stopBtn.getAttribute("data-csrf"));
    var bar = document.getElementById("enroll-bar");
    var barWrap = document.getElementById("enroll-progress");
    var curLine = document.getElementById("enroll-current");
    var feed = document.getElementById("enroll-feed");
    var idle = document.getElementById("enroll-idle");
    var kindEl = document.getElementById("run-kind");
    var kindText = document.getElementById("run-kind-text");
    var phaseEl = document.getElementById("run-phase");
    var countEl = document.getElementById("run-count");
    var seen = {};
    var polling = false;
    var notified = false;

    function setTally(b) {
      ["enrolled", "already", "expired", "failed", "filtered"].forEach(function (k) {
        var el = document.getElementById("t-" + k);
        if (el) el.textContent = b[k] || 0;
      });
    }
    function pushFeed(items) {
      if (!feed || !items) return;
      items.slice().reverse().forEach(function (it) {
        var key = (it.title || "") + "|" + (it.at || "");
        if (seen[key]) return; seen[key] = 1;
        var li = document.createElement("li");
        li.innerHTML = '<span class="check">&#10003;</span><span class="t"></span><span class="tags"></span>';
        li.querySelector(".t").textContent = it.title || "Course";
        [it.category, it.language].forEach(function (v) {
          if (!v) return;
          var t = document.createElement("span"); t.className = "tag"; t.textContent = v;
          li.querySelector(".tags").appendChild(t);
        });
        feed.appendChild(li); feed.scrollTop = feed.scrollHeight;
      });
    }
    function setKind(kind) {
      if (!kindEl) return;
      var auto = kind === "auto";
      kindEl.className = "run-kind " + (auto ? "auto" : "manual");
      kindText.textContent = auto ? "Auto-enroll run" : "Manual run";
    }
    function runningUI() {
      if (idle) idle.style.display = "none";
      enrollPanel.style.display = "";
      if (barWrap) barWrap.classList.add("busy");
      if (stopBtn) stopBtn.style.display = "";
      if (enrollBtn) { enrollBtn.disabled = true; enrollBtn.textContent = "Working..."; }
    }
    function doneUI() {
      polling = false;
      if (barWrap) barWrap.classList.remove("busy");
      if (stopBtn) stopBtn.style.display = "none";
      if (curLine) curLine.textContent = "";
      if (enrollBtn) { enrollBtn.disabled = false; enrollBtn.textContent = "Enroll now"; }
    }

    async function poll() {
      var res;
      try { res = await (await fetch("/api/enroll-now/status", { headers: { "X-Requested-With": "XMLHttpRequest" } })).json(); }
      catch (e) { if (polling) setTimeout(poll, 1500); return; }

      var pct = res.total ? Math.round((res.done / res.total) * 100) : 0;
      if (bar) bar.style.width = Math.max(pct, res.status === "running" ? 4 : pct) + "%";
      setTally(res); pushFeed(res.recent);
      setKind(res.kind);

      if (res.status === "running") {
        if (phaseEl) phaseEl.textContent = res.phase || "Working...";
        if (countEl) countEl.textContent = res.total ? (res.done + " / " + res.total + " courses · " + pct + "%") : "";
        if (curLine) curLine.innerHTML = res.current
          ? '<span class="spinner"></span> ' + escapeHtml(res.current)
          : '<span class="spinner"></span> ' + escapeHtml(res.phase || "Working...");
        setTimeout(poll, 1200);
      } else {
        if (bar) bar.style.width = "100%";
        if (phaseEl) phaseEl.textContent = res.status === "stopped" ? "Stopped" : "Finished";
        doneUI();
        var n = res.enrolled || 0;
        if (!notified) {
          notified = true;
          var who = res.kind === "auto" ? "Auto-enroll" : "Run";
          if (res.status === "stopped") window.toast("Stopped. " + n + " enrolled this run.", "info");
          else if (n > 0) { window.toast(who + ": " + n + " new course" + (n === 1 ? "" : "s") + " enrolled!", "ok"); window.confettiBurst(); }
          else if ((res.errors || []).length) window.toast(res.errors[0], "bad");
          else window.toast(who + " finished - no new courses right now.", "info");
          // Rings/gauge/milestones/chart are server-rendered - refresh once something changed.
          if (n > 0) setTimeout(function () { location.reload(); }, 1600);
        }
      }
    }

    // Start following a run that is already going (auto tick, or another tab).
    ueResume = function () {
      if (polling) return;
      notified = false; seen = {};
      if (feed) feed.innerHTML = "";
      runningUI(); polling = true; poll();
    };

    if (enrollBtn) enrollBtn.addEventListener("click", async function () {
      if (polling) return;
      notified = false; seen = {};
      if (feed) feed.innerHTML = "";
      runningUI(); setKind("manual");
      if (phaseEl) phaseEl.textContent = "Fetching the latest free courses";
      if (bar) bar.style.width = "4%";
      var r = await window.postForm("/api/enroll-now", { csrf_token: csrf });
      if (r.json && r.json.ok === false) { window.toast(r.json.error || "Could not start.", "bad"); doneUI(); return; }
      polling = true; poll();
    });

    if (stopBtn) stopBtn.addEventListener("click", async function () {
      stopBtn.disabled = true; stopBtn.textContent = "Stopping...";
      await window.postForm("/api/enroll-now/stop", { csrf_token: csrf });
      window.toast("Stopping after the current course...", "info");
      setTimeout(function () { stopBtn.disabled = false; stopBtn.textContent = "Stop"; }, 1500);
    });

    (async function initEnroll() {
      try {
        var s = await (await fetch("/api/enroll-now/status", { headers: { "X-Requested-With": "XMLHttpRequest" } })).json();
        if (s.status === "running") { ueResume(); notified = true; }
      } catch (e) {}
    })();
  }

  /* ── Live engine indicator (topbar, every page) ──────────────────────── */
  (function () {
    var pill = document.getElementById("engine-pill");
    if (!pill) return;
    var title = document.getElementById("eng-title");
    var sub = document.getElementById("eng-sub");
    var fill = document.getElementById("eng-fill");
    var nextAt = null, lastRunning = false, timer = null;

    function fmt(sec) {
      sec = Math.max(0, Math.round(sec));
      return Math.floor(sec / 60) + ":" + ("0" + (sec % 60)).slice(-2);
    }
    function tick() {
      if (nextAt === null) return;
      var left = (nextAt - Date.now()) / 1000;
      var txt = left > 0 ? fmt(left) : "any moment";
      if (pill.getAttribute("data-state") === "idle") sub.textContent = "Next run in " + txt;
      var ic = document.getElementById("idle-countdown");
      if (ic) ic.textContent = txt;
      if (left <= -3) { nextAt = null; schedule(1500); }   // should have started: check now
    }
    setInterval(tick, 1000);

    function schedule(ms) { clearTimeout(timer); timer = setTimeout(refresh, ms); }

    async function refresh() {
      var s;
      try { s = await (await fetch("/api/engine", { headers: { "X-Requested-With": "XMLHttpRequest" } })).json(); }
      catch (e) { schedule(15000); return; }

      if (s.running) {
        var pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
        pill.setAttribute("data-state", "running");
        title.textContent = (s.kind === "auto" ? "Auto-enrolling" : "Enrolling") + (s.total ? " · " + pct + "%" : "");
        sub.textContent = s.current || s.phase || "Working...";
        fill.style.width = (s.total ? Math.max(3, pct) : 3) + "%";
        pill.title = (s.phase || "") + (s.total ? " (" + s.done + "/" + s.total + ", " + s.enrolled + " enrolled)" : "");
        nextAt = null;
        if (!lastRunning && ueResume) ueResume();   // a run just started: follow it on the dashboard
        lastRunning = true;
        schedule(2500);
      } else {
        lastRunning = false;
        fill.style.width = "0%";
        if (!s.enabled) {
          pill.setAttribute("data-state", "off");
          title.textContent = "Auto-enroll off";
          sub.textContent = "Turn it on in the dashboard";
          nextAt = null;
          schedule(30000);
        } else {
          pill.setAttribute("data-state", "idle");
          title.textContent = "Auto-enroll on";
          nextAt = s.next_in != null ? Date.now() + s.next_in * 1000 : null;
          if (nextAt === null) sub.textContent = "Waiting for first run";
          pill.title = s.last_result ? "Last run: " + s.last_result : "Auto-enroll engine";
          tick();
          schedule(s.next_in != null && s.next_in < 20 ? 3000 : 20000);
        }
      }
    }
    refresh();
    window.ueEngineRefresh = function () { schedule(300); };
  })();

  /* ── Auto-enroll toggle (instant) ────────────────────────────────────── */
  var autoToggle = document.getElementById("auto-toggle");
  if (autoToggle) {
    autoToggle.addEventListener("change", async function () {
      await window.postForm("/settings/auto-enroll", { csrf_token: autoToggle.getAttribute("data-csrf"), enabled: autoToggle.checked ? "1" : "0" });
      var chip = document.getElementById("auto-chip");
      if (chip) { chip.textContent = autoToggle.checked ? "On" : "Off"; chip.className = "chip " + (autoToggle.checked ? "chip-on" : "chip-off"); }
      window.toast(autoToggle.checked ? "Auto-enroll turned on." : "Auto-enroll turned off.", "ok");
      var it = document.getElementById("idle-engine-text");
      if (it) it.innerHTML = autoToggle.checked
        ? 'Auto-enroll is on - next run in <b id="idle-countdown">--:--</b>. Or click "Enroll now".'
        : 'Auto-enroll is off. Click "Enroll now", or turn auto-enroll on.';
      if (window.ueEngineRefresh) window.ueEngineRefresh();
    });
  }
})();
