// papertrader web app: refresh, service worker, and the line charts (drawn to the screen's
// real width, with a crosshair and tooltip on touch or hover). No outside libraries.
"use strict";

(function () {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  }

  document.querySelectorAll("[data-action=reload]").forEach(function (button) {
    button.addEventListener("click", function () { window.location.reload(); });
  });

  // Pages with live numbers reload themselves while they are on screen.
  var every = parseInt(document.body.dataset.autorefresh || "0", 10);
  if (every > 0) {
    setInterval(function () {
      if (document.visibilityState === "visible") { window.location.reload(); }
    }, every * 1000);
  }

  var css = getComputedStyle(document.documentElement);
  function token(name) { return css.getPropertyValue(name).trim(); }
  var NS = "http://www.w3.org/2000/svg";
  function el(tag, attrs, parent) {
    var node = document.createElementNS(NS, tag);
    Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (parent) { parent.appendChild(node); }
    return node;
  }
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
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
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
    var height = spec.height || 210;
    var m = { top: 10, right: 14, bottom: 24, left: 48 };
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
    var muted = token("--muted"), grid = token("--grid"), axis = token("--axis"), surface = token("--surface");
    for (var v = y0; v <= y1 + step / 2; v += step) {
      el("line", { x1: m.left, x2: width - m.right, y1: Y(v), y2: Y(v), stroke: Math.abs(v) < step / 1000 && spec.zero ? axis : grid, "stroke-width": 1 }, svg);
      var t = el("text", { x: m.left - 6, y: Y(v) + 4, "text-anchor": "end", fill: muted, "font-size": 11 }, svg);
      t.textContent = fmt(Math.abs(v) < step / 1000 ? 0 : v, spec.unit);
    }
    var ticks = Math.max(2, Math.min(5, Math.floor((width - m.left - m.right) / 80)));
    for (var i = 0; i <= ticks; i++) {
      var tt = x0 + (x1 - x0) * i / ticks;
      var lab = el("text", { x: X(tt), y: height - 6, "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle", fill: muted, "font-size": 11 }, svg);
      lab.textContent = label(new Date(tt));
    }
    series.forEach(function (s) {
      var color = token("--s" + s.slot);
      var path = s.points.map(function (p, k) { return (k ? "L" : "M") + X(day(p[0]).getTime()).toFixed(1) + " " + Y(p[1]).toFixed(1); }).join(" ");
      if (spec.area && series.length === 1) {
        var last = s.points[s.points.length - 1], first = s.points[0];
        el("path", { d: path + " L" + X(day(last[0]).getTime()) + " " + Y(Math.max(y0, Math.min(0, y1))) + " L" + X(day(first[0]).getTime()) + " " + Y(Math.max(y0, Math.min(0, y1))) + " Z", fill: color, "fill-opacity": 0.1, stroke: "none" }, svg);
      }
      el("path", { d: path, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      var end = s.points[s.points.length - 1];
      el("circle", { cx: X(day(end[0]).getTime()), cy: Y(end[1]), r: 4, fill: color, stroke: surface, "stroke-width": 2 }, svg);
    });
    // Crosshair and tooltip.
    var cross = el("line", { y1: m.top, y2: height - m.bottom, stroke: axis, "stroke-width": 1, visibility: "hidden" }, svg);
    var marks = series.map(function (s) { return el("circle", { r: 4, fill: token("--s" + s.slot), stroke: surface, "stroke-width": 2, visibility: "hidden" }, svg); });
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
        var sw = document.createElement("i"); sw.style.background = token("--s" + s.slot); line.appendChild(sw);
        line.appendChild(document.createTextNode(s.name + "  " + tipValue(point[1], spec.unit)));
        tip.appendChild(line);
      });
      tip.hidden = false;
      var left = X(best) + 12;
      if (left + tip.offsetWidth > width) { left = X(best) - tip.offsetWidth - 12; }
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = (holder.offsetTop + 8) + "px";
    }
    function hide() { tip.hidden = true; cross.setAttribute("visibility", "hidden"); marks.forEach(function (c) { c.setAttribute("visibility", "hidden"); }); }
    svg.addEventListener("pointermove", show);
    svg.addEventListener("pointerdown", show);
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("touchstart", show, { passive: true });
  }

  var figures = Array.from(document.querySelectorAll("figure.chart"));
  figures.forEach(draw);
  var timer = null;
  window.addEventListener("resize", function () {
    clearTimeout(timer);
    timer = setTimeout(function () { figures.forEach(draw); }, 150);
  });
})();
