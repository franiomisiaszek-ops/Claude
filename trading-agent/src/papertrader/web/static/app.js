// papertrader web app: smooth page changes (no white flash), pull down to refresh, live
// pages that refresh in place, the line charts, and the Avanza ticks. No outside libraries.
"use strict";

(function () {
  var NS = "http://www.w3.org/2000/svg";
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var refreshTimer = null;
  var busy = false;
  var last = Date.now();
  var reg = null;
  var root = document.documentElement;

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").then(function (r) { reg = r; }).catch(function () {});
  }
  // The home-screen app resumes from memory: fresh numbers when it comes back after a while.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState !== "visible") { return; }
    if (reg) { reg.update().catch(function () {}); }
    if (Date.now() - last > 30000 && document.querySelector("nav.tabs")) { swap(location.href, { keepScroll: true, auto: true }); }
  });
  window.addEventListener("pageshow", function (e) {
    if (e.persisted && document.querySelector("nav.tabs")) { swap(location.href, { keepScroll: true, auto: true }); }
  });

  // ------------------------------------------------------------------ helpers
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
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
    var bar = document.querySelector(".loading");
    if (!bar) { return; }
    bar.className = on ? "loading on" : "loading done";
    if (!on) { setTimeout(function () { bar.className = "loading"; }, 600); }
  }

  // ------------------------------------------------------------------ smooth page changes
  function sameApp(url) {
    return url.origin === location.origin && !/^\/(login|logout|mode|static|sw\.js|manifest)/.test(url.pathname);
  }

  function swap(url, opts) {
    opts = opts || {};
    if (busy) { return Promise.resolve(); }
    if (opts.auto) {  // never refresh under your finger or while you type
      var f = document.activeElement;
      if ((f && /^(INPUT|SELECT|TEXTAREA)$/.test(f.tagName)) || document.querySelector(".tip:not([hidden])")) { return Promise.resolve(); }
    }
    busy = true;
    loading(true);
    var keep = opts.keepScroll ? window.scrollY : 0;
    var open = opts.keepScroll ? Array.from(document.querySelectorAll("main details")).map(function (d) { return d.open; }) : [];
    return fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "swap" } }).then(function (res) {
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
      newMain.querySelectorAll("details").forEach(function (d, i) { if (i < open.length) { d.open = open[i]; } });
      document.title = doc.title;
      document.querySelector("main").replaceWith(newMain);
      ["header.top", "nav.tabs", ".flash"].forEach(function (sel) {
        var old = document.querySelector(sel), fresh = doc.querySelector(sel);
        if (old && fresh) { old.replaceWith(fresh); } else if (old && !fresh) { old.remove(); }
        else if (!old && fresh && sel === ".flash") { document.querySelector("main").before(fresh); }
      });
      document.body.dataset.autorefresh = doc.body.dataset.autorefresh || "";
      if (opts.push) { history.pushState({ swap: true }, "", page.url.pathname + page.url.search); }
      window.scrollTo(0, keep);
      last = Date.now();
      root.classList.remove("stale");
      start();
    }).catch(function () {
      if (opts.keepScroll) { root.classList.add("stale"); }  // a refresh failed: keep what is on screen, say so
      else { location.assign(url); }  // a page change failed: let the browser (and the offline page) handle it
    }).then(function () { busy = false; loading(false); });
  }

  document.addEventListener("click", function (evt) {
    var link = evt.target.closest ? evt.target.closest("a[href]") : null;
    if (!link || evt.defaultPrevented || evt.metaKey || evt.ctrlKey || evt.shiftKey || link.target || link.hasAttribute("download")) { return; }
    var url = new URL(link.href, location.href);
    if (!sameApp(url)) { return; }
    evt.preventDefault();
    if (link.closest(".tabs") && link.classList.contains("on") && url.pathname === location.pathname) {
      // tapping the tab you are on: back to the top, or refresh when already there (like iOS apps)
      if (window.scrollY > 0) { window.scrollTo({ top: 0, behavior: "smooth" }); } else { swap(location.href, { keepScroll: true }); }
      return;
    }
    swap(url.href, { push: true });
  });
  document.addEventListener("touchstart", function () {}, { passive: true });  // makes :active work on iOS

  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if ((form.method || "get").toLowerCase() !== "get") { return; }  // forms that change something load normally
    var url = new URL(form.action, location.href);
    if (!sameApp(url)) { return; }
    evt.preventDefault();
    url.search = new URLSearchParams(new FormData(form)).toString();
    if (document.activeElement) { document.activeElement.blur(); }  // closes the iPhone keyboard
    swap(url.href, { push: true });
  });

  window.addEventListener("popstate", function () { swap(location.href, {}); });

  // ------------------------------------------------------------------ pull down to refresh
  (function () {
    var startY = null, pulled = 0;
    function hint(px) {
      var box = document.querySelector(".pull");
      if (box) { box.style.transform = "translate(-50%, " + Math.min(px - 60, 10) + "px)"; box.textContent = px > 70 ? "Release to refresh" : "Pull to refresh"; }
    }
    document.addEventListener("touchstart", function (e) {
      startY = window.scrollY <= 0 && e.touches.length === 1 ? e.touches[0].clientY : null;
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
    var every = parseInt(document.body.dataset.autorefresh || "0", 10);
    if (every > 0) {
      refreshTimer = setInterval(function () {
        if (document.visibilityState === "visible" && Date.now() - last >= every * 1000) { swap(location.href, { keepScroll: true, auto: true }); }
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
    document.querySelectorAll("figure.chart").forEach(draw);
  }

  // ------------------------------------------------------------------ charts
  function niceStep(span, count) {
    var raw = span / Math.max(1, count);
    var power = Math.pow(10, Math.floor(Math.log10(raw)));
    var unit = raw / power;
    return (unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 2.5 ? 2.5 : unit <= 5 ? 5 : 10) * power;
  }
  function fmt(value, unit) {
    if (unit === "$") {
      var a = Math.abs(value);
      var text = a >= 10000 ? (a / 1000).toFixed(a >= 100000 ? 0 : 1) + "K" : a.toLocaleString(undefined, { maximumFractionDigits: a < 10 ? 2 : 0 });
      return (value < 0 ? "-$" : "$") + text;
    }
    if (unit === "%") { return (value > 0 ? "+" : "") + value.toFixed(Math.abs(value) < 10 ? 1 : 0) + "%"; }
    return value.toFixed(2);
  }
  function tipValue(value, unit) {
    if (unit === "$") { return (value < 0 ? "-$" : "$") + Math.abs(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
    if (unit === "%") { return (value > 0 ? "+" : "") + value.toFixed(2) + "%"; }
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
    var width = Math.max(260, holder.clientWidth);
    var height = spec.height || 220;
    var m = { top: 12, right: 16, bottom: 26, left: 50 };
    var xs = [], ys = [];
    series.forEach(function (s) { s.points.forEach(function (p) { xs.push(day(p[0]).getTime()); ys.push(p[1]); }); });
    if (spec.zero) { ys.push(0); }
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
    if (x0 === x1) { x0 -= 86400000; x1 += 86400000; }
    var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    if (y0 === y1) { y0 -= 1; y1 += 1; }
    var step = niceStep(y1 - y0, 4);
    y0 = Math.floor(y0 / step) * step; y1 = Math.ceil(y1 / step) * step;
    function X(t) { return m.left + (t - x0) / (x1 - x0) * (width - m.left - m.right); }
    function Y(v) { return m.top + (1 - (v - y0) / (y1 - y0)) * (height - m.top - m.bottom); }
    var svg = el("svg", { viewBox: "0 0 " + width + " " + height, width: width, height: height, role: "img", "aria-label": spec.title || "chart" }, holder);
    var muted = css("--muted"), grid = css("--grid"), axis = css("--axis"), surface = css("--surface");
    for (var v = y0; v <= y1 + step / 2; v += step) {
      var isZero = Math.abs(v) < step / 1000;
      el("line", { x1: m.left, x2: width - m.right, y1: Y(v), y2: Y(v), stroke: isZero && spec.zero ? axis : grid, "stroke-width": 1 }, svg);
      var t = el("text", { x: m.left - 8, y: Y(v) + 4, "text-anchor": "end", fill: muted, "font-size": 12 }, svg);
      t.textContent = fmt(isZero ? 0 : v, spec.unit);
    }
    var ticks = Math.max(2, Math.min(5, Math.floor((width - m.left - m.right) / 80)));
    for (var i = 0; i <= ticks; i++) {
      var tt = x0 + (x1 - x0) * i / ticks;
      var lab = el("text", { x: X(tt), y: height - 6, "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle", fill: muted, "font-size": 12 }, svg);
      lab.textContent = label(new Date(tt));
    }
    series.forEach(function (s) {
      var color = css("--s" + s.slot);
      var path = s.points.map(function (p, k) { return (k ? "L" : "M") + X(day(p[0]).getTime()).toFixed(1) + " " + Y(p[1]).toFixed(1); }).join(" ");
      if (spec.area && series.length === 1) {
        var last = s.points[s.points.length - 1], first = s.points[0], base = Y(Math.max(y0, Math.min(0, y1)));
        el("path", { d: path + " L" + X(day(last[0]).getTime()) + " " + base + " L" + X(day(first[0]).getTime()) + " " + base + " Z", fill: color, "fill-opacity": 0.12, stroke: "none" }, svg);
      }
      el("path", { d: path, fill: "none", stroke: color, "stroke-width": 2.25, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      var end = s.points[s.points.length - 1];
      el("circle", { cx: X(day(end[0]).getTime()), cy: Y(end[1]), r: 4, fill: color, stroke: surface, "stroke-width": 2 }, svg);
    });
    var cross = el("line", { y1: m.top, y2: height - m.bottom, stroke: axis, "stroke-width": 1, visibility: "hidden" }, svg);
    var marks = series.map(function (s) { return el("circle", { r: 5, fill: css("--s" + s.slot), stroke: surface, "stroke-width": 2, visibility: "hidden" }, svg); });
    var tip = figure.querySelector(".tip");
    var days = Array.from(new Set(xs)).sort(function (a, b) { return a - b; });
    function show(evt) {
      var box = svg.getBoundingClientRect();
      var px = (evt.touches ? evt.touches[0].clientX : evt.clientX) - box.left;
      var t = x0 + (px - m.left) / (width - m.left - m.right) * (x1 - x0);
      var best = days.reduce(function (a, b) { return Math.abs(b - t) < Math.abs(a - t) ? b : a; }, days[0]);
      cross.setAttribute("x1", X(best)); cross.setAttribute("x2", X(best)); cross.setAttribute("visibility", "visible");
      tip.textContent = "";
      var head = document.createElement("b"); head.textContent = label(new Date(best)) + " " + new Date(best).getFullYear(); tip.appendChild(head);
      series.forEach(function (s, k) {
        var point = s.points.find(function (p) { return day(p[0]).getTime() === best; });
        if (!point) { marks[k].setAttribute("visibility", "hidden"); return; }
        marks[k].setAttribute("cx", X(best)); marks[k].setAttribute("cy", Y(point[1])); marks[k].setAttribute("visibility", "visible");
        var line = document.createElement("div");
        var sw = document.createElement("i"); sw.style.background = css("--s" + s.slot); line.appendChild(sw);
        line.appendChild(document.createTextNode(s.name + "  " + tipValue(point[1], spec.unit)));
        tip.appendChild(line);
      });
      tip.hidden = false;
      var left = X(best) + 14;
      if (left + tip.offsetWidth > width) { left = X(best) - tip.offsetWidth - 14; }
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = (holder.offsetTop - tip.offsetHeight - 6 > 0 ? holder.offsetTop - tip.offsetHeight - 6 : holder.offsetTop + 4) + "px";
    }
    function hide() { tip.hidden = true; cross.setAttribute("visibility", "hidden"); marks.forEach(function (c) { c.setAttribute("visibility", "hidden"); }); }
    svg.addEventListener("pointermove", show);
    svg.addEventListener("pointerdown", show);
    svg.addEventListener("pointerleave", function (e) { if (e.pointerType === "mouse") { hide(); } });
    svg.addEventListener("touchstart", show, { passive: true });
    svg.addEventListener("touchend", function () { setTimeout(hide, 1800); }, { passive: true });
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
