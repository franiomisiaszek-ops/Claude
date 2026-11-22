// papertrader web app: smooth page changes (no white flash), pull down to refresh, live
// pages that refresh in place, the line charts, and the Avanza ticks. No outside libraries.
"use strict";

(function () {
  var NS = "http://www.w3.org/2000/svg";
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var MINUS = "−";
  var root = document.documentElement;
  var refreshTimer = null;
  var inflight = null;
  var loadedAt = Date.now();
  var reg = null;
  if ("scrollRestoration" in history) { history.scrollRestoration = "manual"; }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").then(function (r) { reg = r; }).catch(function () {});
  }

  // ------------------------------------------------------------------ helpers
  function css(name) { return getComputedStyle(root).getPropertyValue(name).trim(); }
  function el(tag, attrs, parent) {
    var node = document.createElementNS(NS, tag);
    Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (parent) { parent.appendChild(node); }
    return node;
  }
  function store(key, value) {
    try {
      if (value === undefined) { return window.localStorage.getItem(key); }
      if (value === null) { window.localStorage.removeItem(key); } else { window.localStorage.setItem(key, value); }
    } catch (e) { return null; }
    return null;
  }
  function loading(on) {
    root.classList.toggle("is-loading", on);
    var bar = document.querySelector(".loading");
    if (!bar) { return; }
    bar.className = on ? "loading on" : "loading done";
    if (!on) { setTimeout(function () { bar.className = "loading"; }, 600); }
  }

  // ------------------------------------------------------------------ smooth page changes
  function sameApp(url) {
    return url.origin === location.origin && !/^\/(login|logout|mode|static|sw\.js|manifest)/.test(url.pathname);
  }

  // opts: push (a new page), keepScroll (refresh in place), background (a refresh nobody asked for), y (scroll to)
  function swap(url, opts) {
    opts = opts || {};
    if (inflight) {
      if (opts.background) { return Promise.resolve(); }
      inflight.abort();  // a tap always wins over a refresh in progress
    }
    if (opts.background) {  // never refresh under your finger or while you type
      var f = document.activeElement;
      if ((f && /^(INPUT|SELECT|TEXTAREA)$/.test(f.tagName)) || document.querySelector(".tip:not([hidden])") || root.classList.contains("scrubbing")) {
        return Promise.resolve();
      }
    }
    var ctrl = inflight = new AbortController();
    var keep = window.scrollY, open = {};
    document.querySelectorAll("main details[id]").forEach(function (d) { open[d.id] = d.open; });
    if (opts.push) { history.replaceState({ swap: true, y: window.scrollY }, ""); }
    loading(true);
    return fetch(url, { credentials: "same-origin", cache: "no-store", signal: ctrl.signal, headers: { "X-Requested-With": "swap" } }).then(function (res) {
      var landed = new URL(res.url);
      if (!res.ok || landed.pathname === "/login") { throw new Error("full load"); }
      return res.text().then(function (html) { return { html: html, url: landed }; });
    }).then(function (page) {
      var doc = new DOMParser().parseFromString(page.html, "text/html");
      var newMain = doc.querySelector("main");
      if (!newMain) { throw new Error("full load"); }
      var sheet = 'link[rel="stylesheet"]', fresh = doc.querySelector(sheet);
      if (!fresh || fresh.getAttribute("href") !== document.querySelector(sheet).getAttribute("href")) {
        location.assign(page.url.href);  // a new version of the app: load it properly
        return;
      }
      if (opts.keepScroll) {
        newMain.querySelectorAll("details[id]").forEach(function (d) { if (d.id in open) { d.open = open[d.id]; } });
      }
      document.title = doc.title;
      document.querySelector("main").replaceWith(newMain);
      ["header.top", "nav.tabs", ".flash"].forEach(function (sel) {
        var old = document.querySelector(sel), next = doc.querySelector(sel);
        if (old && next) { old.replaceWith(next); } else if (old && !next) { old.remove(); }
        else if (!old && next && sel === ".flash") { document.querySelector("main").before(next); }
      });
      document.body.dataset.autorefresh = doc.body.dataset.autorefresh || "";
      if (opts.push) { history.pushState({ swap: true, y: 0 }, "", page.url.pathname + page.url.search); }
      window.scrollTo(0, opts.keepScroll ? keep : (opts.y || 0));
      loadedAt = Date.now();
      root.classList.remove("stale");
      start();
      if (opts.push) {  // tell screen readers where they are
        var h1 = document.querySelector("main h1");
        if (h1) { h1.setAttribute("tabindex", "-1"); h1.focus({ preventScroll: true }); }
      }
    }).catch(function (e) {
      if (e && e.name === "AbortError") { return; }
      if (opts.keepScroll) { root.classList.add("stale"); }  // a refresh failed: keep what is on screen, say so
      else { location.assign(url); }  // a page change failed: let the browser and the offline page handle it
    }).then(function () {
      if (inflight === ctrl) { inflight = null; loading(false); }
    });
  }

  document.addEventListener("click", function (evt) {
    var link = evt.target.closest ? evt.target.closest("a[href]") : null;
    if (!link || evt.defaultPrevented || evt.metaKey || evt.ctrlKey || evt.shiftKey || link.target || link.hasAttribute("download")) { return; }
    var url = new URL(link.href, location.href);
    if (!sameApp(url)) { return; }
    evt.preventDefault();
    var tab = link.closest(".tabs");
    if (tab && link.classList.contains("on") && url.pathname === location.pathname) {
      // tapping the tab you are on: back to the top, or refresh when already there (like iOS apps)
      if (window.scrollY > 0) { window.scrollTo({ top: 0, behavior: "smooth" }); } else { swap(location.href, { keepScroll: true }); }
      return;
    }
    if (tab) {  // show the tap at once, even on a slow connection
      tab.querySelectorAll("a.on").forEach(function (a) { a.classList.remove("on"); });
      link.classList.add("on");
    }
    swap(url.href, { push: true });
  });

  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if ((form.method || "get").toLowerCase() !== "get") {  // forms that change something load normally, once
      var button = evt.submitter;
      if (button) { setTimeout(function () { button.disabled = true; button.textContent = "Working…"; }, 0); }
      loading(true);
      return;
    }
    var url = new URL(form.action, location.href);
    if (!sameApp(url)) { return; }
    evt.preventDefault();
    url.search = new URLSearchParams(new FormData(form)).toString();
    if (document.activeElement) { document.activeElement.blur(); }  // closes the iPhone keyboard
    swap(url.href, { push: true });
  });

  window.addEventListener("popstate", function (e) { swap(location.href, { y: (e.state && e.state.y) || 0 }); });
  document.addEventListener("touchstart", function () {}, { passive: true });  // makes :active work on iOS

  // The home-screen app resumes from memory: fresh numbers when it comes back after a while.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState !== "visible") { return; }
    if (reg) { reg.update().catch(function () {}); }
    var every = parseInt(document.body.dataset.autorefresh || "0", 10) * 1000;
    if (document.querySelector("nav.tabs") && Date.now() - loadedAt > Math.min(every || 30000, 30000)) {
      swap(location.href, { keepScroll: true, background: true });
    }
  });
  window.addEventListener("pageshow", function (e) {
    if (e.persisted && document.querySelector("nav.tabs")) { swap(location.href, { keepScroll: true, background: true }); }
  });

  // ------------------------------------------------------------------ pull down to refresh
  (function () {
    var startY = null, pulled = 0;
    function hint(px) {
      var box = document.querySelector(".pull");
      if (box) { box.style.transform = "translate(-50%, " + Math.min(px - 60, 10) + "px)"; box.textContent = px > 70 ? "Release to refresh" : "Pull to refresh"; }
    }
    document.addEventListener("touchstart", function (e) {
      startY = window.scrollY <= 0 && e.touches.length === 1 && !e.target.closest("figure.chart") ? e.touches[0].clientY : null;
      pulled = 0;
    }, { passive: true });
    document.addEventListener("touchmove", function (e) {
      if (startY === null) { return; }
      pulled = e.touches[0].clientY - startY;
      if (pulled > 10) { hint(pulled); }
    }, { passive: true });
    document.addEventListener("touchend", function () {
      if (startY !== null && pulled > 70) { swap(location.href, { keepScroll: true }); }
      startY = null;
      hint(0);
    });
  })();

  // ------------------------------------------------------------------ the parts of a page
  function start() {
    document.querySelectorAll("[data-action=reload]").forEach(function (button) {
      button.onclick = function () { swap(location.href, { keepScroll: true }); };
    });
    clearInterval(refreshTimer);
    var every = parseInt(document.body.dataset.autorefresh || "0", 10) * 1000;
    if (every > 0) {
      refreshTimer = setInterval(function () {
        if (document.visibilityState === "visible" && Date.now() - loadedAt >= every) { swap(location.href, { keepScroll: true, background: true }); }
      }, 5000);
    }
    document.querySelectorAll("input[data-done]").forEach(function (box) {
      var key = box.dataset.done, row = box.closest(".match-row");
      box.checked = store(key) === "1";
      if (row) { row.classList.toggle("done", box.checked); }
      box.onchange = function () {
        store(key, box.checked ? "1" : null);
        if (row) { row.classList.toggle("done", box.checked); }
      };
    });
    document.querySelectorAll("select[data-autosubmit]").forEach(function (s) {
      s.onchange = function () { if (s.form.requestSubmit) { s.form.requestSubmit(); } else { s.form.submit(); } };
    });
    document.querySelectorAll("figure.chart").forEach(draw);
  }

  // ------------------------------------------------------------------ charts
  function niceStep(span, count) {
    var raw = span / Math.max(1, count);
    var power = Math.pow(10, Math.floor(Math.log10(raw)));
    var unit = raw / power;
    return (unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 2.5 ? 2.5 : unit <= 5 ? 5 : 10) * power;
  }
  function sign(value) { return value > 0 ? "+" : value < 0 ? MINUS : ""; }
  function fmt(value, unit) {
    var a = Math.abs(value);
    if (unit === "$") {
      var text = a >= 10000 ? (a / 1000).toFixed(a >= 100000 ? 0 : 1) + "K" : a.toLocaleString(undefined, { maximumFractionDigits: a < 10 ? 2 : 0 });
      return (value < 0 ? MINUS : "") + "$" + text;
    }
    if (unit === "%") { return sign(value) + a.toFixed(a < 10 ? 1 : 0) + "%"; }
    return value.toFixed(2);
  }
  function exact(value, unit) {
    var a = Math.abs(value);
    if (unit === "$") { return (value < 0 ? MINUS : "") + "$" + a.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
    if (unit === "%") { return sign(value) + a.toFixed(2) + "%"; }
    return value.toFixed(3);
  }
  function day(text) { return new Date(text + "T12:00:00"); }
  function label(date) { return date.getDate() + " " + MONTHS[date.getMonth()]; }

  function draw(figure) {
    var spec = JSON.parse(figure.dataset.chart);
    var series = spec.series.filter(function (s) { return s.points.length > 0; });
    var holder = figure.querySelector(".plot");
    holder.textContent = "";
    if (!series.length) {
      var empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = spec.empty || "No data yet.";
      holder.appendChild(empty);
      return;
    }
    var colors = series.map(function (s) { return css("--s" + s.slot); });
    var width = Math.max(260, holder.clientWidth);
    var height = spec.height || 220;
    var m = { top: 12, right: 16, bottom: 26, left: 52 };
    // Trading days, evenly spaced: weekends and holidays leave no gaps.
    var days = [], ys = [];
    series.forEach(function (s) { s.points.forEach(function (p) { if (days.indexOf(p[0]) < 0) { days.push(p[0]); } ys.push(p[1]); }); });
    days.sort();
    var index = {};
    days.forEach(function (d, i) { index[d] = i; });
    if (spec.zero) { ys.push(0); }
    var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    if (y0 === y1) { y0 -= 1; y1 += 1; }
    var step = niceStep(y1 - y0, 4);
    y0 = Math.floor(y0 / step) * step; y1 = Math.ceil(y1 / step) * step;
    var span = width - m.left - m.right;
    function X(d) { return m.left + (days.length > 1 ? index[d] / (days.length - 1) : 0.5) * span; }
    function Y(v) { return m.top + (1 - (v - y0) / (y1 - y0)) * (height - m.top - m.bottom); }
    var svg = el("svg", { viewBox: "0 0 " + width + " " + height, width: width, height: height, role: "img", "aria-label": spec.title || "chart" }, holder);
    var muted = css("--muted"), grid = css("--grid"), axis = css("--axis"), ring = css(figure.closest(".card") ? "--surface" : "--page");
    for (var v = y0; v <= y1 + step / 2; v += step) {
      var isZero = Math.abs(v) < step / 1000;
      el("line", { x1: m.left, x2: width - m.right, y1: Y(v), y2: Y(v), stroke: isZero && spec.zero ? axis : grid, "stroke-width": 1 }, svg);
      var t = el("text", { x: m.left - 8, y: Y(v) + 4, "text-anchor": "end", fill: muted, "font-size": 12 }, svg);
      t.textContent = fmt(isZero ? 0 : v, spec.unit);
    }
    var count = Math.max(1, Math.min(4, days.length - 1));
    for (var i = 0; i <= count; i++) {
      var d = days[Math.round(i / count * (days.length - 1))];
      var lab = el("text", { x: X(d), y: height - 6, "text-anchor": i === 0 ? "start" : i === count ? "end" : "middle", fill: muted, "font-size": 12 }, svg);
      lab.textContent = label(day(d));
    }
    series.forEach(function (s, k) {
      var benchmark = s.slot === 3;
      var path = s.points.map(function (p, n) { return (n ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1); }).join(" ");
      if (spec.area && series.length === 1) {
        var last = s.points[s.points.length - 1], first = s.points[0], base = Y(Math.max(y0, Math.min(0, y1)));
        el("path", { d: path + " L" + X(last[0]) + " " + base + " L" + X(first[0]) + " " + base + " Z", fill: colors[k], "fill-opacity": 0.12, stroke: "none" }, svg);
      }
      el("path", { d: path, fill: "none", stroke: colors[k], "stroke-width": benchmark ? 1.75 : 2.25, "stroke-dasharray": benchmark ? "5 4" : "none",
                   "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      var end = s.points[s.points.length - 1];
      el("circle", { cx: X(end[0]), cy: Y(end[1]), r: 4, fill: colors[k], stroke: ring, "stroke-width": 2 }, svg);
    });
    var cross = el("line", { y1: m.top, y2: height - m.bottom, stroke: css("--ink-2"), "stroke-opacity": 0.6, "stroke-width": 1, visibility: "hidden" }, svg);
    var marks = series.map(function (s, k) { return el("circle", { r: 5, fill: colors[k], stroke: ring, "stroke-width": 2, visibility: "hidden" }, svg); });
    var tip = figure.querySelector(".tip");
    var legend = figure.querySelector(".legend");

    function readout(at) {  // values in the legend: the latest, or the day under your finger
      if (!legend) { return; }
      var when = legend.querySelector(".when");
      if (when) { when.textContent = at === null ? "Latest" : label(day(at)) + " " + day(at).getFullYear(); }
      series.forEach(function (s) {
        var slot = legend.querySelector('[data-name="' + s.name + '"] .v');
        if (!slot) { return; }
        var point = at === null ? s.points[s.points.length - 1] : s.points.find(function (p) { return p[0] === at; });
        slot.textContent = point ? exact(point[1], spec.unit) : "–";
      });
    }
    function show(evt) {
      var box = svg.getBoundingClientRect();
      var ratio = Math.max(0, Math.min(1, ((evt.touches ? evt.touches[0].clientX : evt.clientX) - box.left - m.left) / span));
      var best = days[Math.round(ratio * (days.length - 1))];
      cross.setAttribute("x1", X(best)); cross.setAttribute("x2", X(best)); cross.setAttribute("visibility", "visible");
      series.forEach(function (s, k) {
        var point = s.points.find(function (p) { return p[0] === best; });
        if (!point) { marks[k].setAttribute("visibility", "hidden"); return; }
        marks[k].setAttribute("cx", X(best)); marks[k].setAttribute("cy", Y(point[1])); marks[k].setAttribute("visibility", "visible");
      });
      readout(best);
      if (evt.pointerType === "touch") { tip.hidden = true; return; }  // on a phone the legend shows the values
      tip.textContent = "";
      var head = document.createElement("b"); head.textContent = label(day(best)) + " " + day(best).getFullYear(); tip.appendChild(head);
      series.forEach(function (s, k) {
        var point = s.points.find(function (p) { return p[0] === best; });
        if (!point) { return; }
        var line = document.createElement("div");
        var sw = document.createElement("i"); sw.style.background = colors[k]; line.appendChild(sw);
        line.appendChild(document.createTextNode(s.name + "  " + exact(point[1], spec.unit)));
        tip.appendChild(line);
      });
      tip.hidden = false;
      var left = X(best) + 14;
      if (left + tip.offsetWidth > width) { left = X(best) - tip.offsetWidth - 14; }
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = (holder.offsetTop + 4) + "px";
    }
    function hide() {
      tip.hidden = true; cross.setAttribute("visibility", "hidden");
      marks.forEach(function (c) { c.setAttribute("visibility", "hidden"); });
      readout(null);
    }
    // On a phone, scrub only when the finger means it (holds, or moves sideways); otherwise let the page scroll.
    var hold = null, hideT = null, sx = 0, sy = 0, scrub = false;
    svg.addEventListener("pointerdown", function (e) {
      clearTimeout(hideT);
      if (e.pointerType !== "touch") { show(e); return; }
      sx = e.clientX; sy = e.clientY; scrub = false;
      hold = setTimeout(function () { scrub = true; root.classList.add("scrubbing"); show(e); }, 150);
    });
    svg.addEventListener("pointermove", function (e) {
      if (e.pointerType !== "touch") { show(e); return; }
      var dx = Math.abs(e.clientX - sx);
      if (!scrub && dx > 6 && dx > Math.abs(e.clientY - sy)) { clearTimeout(hold); scrub = true; root.classList.add("scrubbing"); }
      if (scrub) { show(e); }
    });
    svg.addEventListener("pointerup", function (e) {
      clearTimeout(hold); scrub = false; root.classList.remove("scrubbing");
      if (e.pointerType === "touch") { hideT = setTimeout(hide, 1500); } else { hide(); }
    });
    svg.addEventListener("pointercancel", function () { clearTimeout(hold); scrub = false; root.classList.remove("scrubbing"); hide(); });
    svg.addEventListener("pointerleave", function (e) { if (e.pointerType !== "touch") { hide(); } });
    readout(null);
  }

  var resizeTimer = null, lastWidth = window.innerWidth;
  window.addEventListener("resize", function () {
    if (window.innerWidth === lastWidth) { return; }
    lastWidth = window.innerWidth;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { document.querySelectorAll("figure.chart").forEach(draw); }, 150);
  });
  start();
})();
