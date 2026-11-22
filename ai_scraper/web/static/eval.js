// Evaluation page: per-extractor micro F1 (dev / held-out), a fixture x extractor matrix and per-field drill-down.
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var data = null, split = "all";
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text != null) e.textContent = text;
    return e;
  }
  function cls(f1) { return "f1 " + (f1 >= 0.95 ? "hi" : f1 >= 0.8 ? "mid" : "lo"); }
  function key(ex) { return split === "all" ? ex : ex + "@" + split; }

  function renderKpis() {
    var k = $("kpis"); k.innerHTML = "";
    data.extractors.forEach(function (ex) {
      var s = data.summary[key(ex)];
      var c = el("div", { class: "card kpi" });
      c.appendChild(el("div", { class: "label" }, ex));
      var v = el("div", { class: "value" }, s ? s.f1.toFixed(3) : "-"); c.appendChild(v);
      c.appendChild(el("div", { class: "sub" }, s ? "P " + s.precision.toFixed(3) + " · R " + s.recall.toFixed(3) + " · " + s.tp + " cells right" : ""));
      k.appendChild(c);
    });
    k.style.gridTemplateColumns = "repeat(" + data.extractors.length + ", minmax(0, 1fr))";
  }
  function renderMatrix() {
    var t = $("matrix"); t.innerHTML = "";
    var hr = el("tr"); hr.appendChild(el("th", null, "Page"));
    data.extractors.forEach(function (ex) { hr.appendChild(el("th", { class: "n" }, ex)); });
    hr.appendChild(el("th", { class: "n" }, "Rows"));
    var th = el("thead"); th.appendChild(hr); t.appendChild(th);
    var tb = el("tbody"), byFx = {}, order = [];
    data.results.forEach(function (r) {
      if (split !== "all" && r.split !== split) return;
      if (!byFx[r.fixture]) { byFx[r.fixture] = {}; order.push(r.fixture); }
      byFx[r.fixture][r.extractor] = r;
    });
    order.sort(function (a, b) {  // dev pages first, then held-out; alphabetical inside each group
      var sa = byFx[a][data.extractors[0]].split, sb = byFx[b][data.extractors[0]].split;
      return sa === sb ? (a < b ? -1 : 1) : (sa === "dev" ? -1 : 1);
    });
    var lastSplit = null;
    order.forEach(function (fx) {
      var any = byFx[fx][data.extractors[0]];
      if (any.split !== lastSplit) {
        var sr = el("tr", { class: "split-row" }); sr.appendChild(el("td", { colspan: String(data.extractors.length + 2) }, any.split === "dev" ? "Dev pages (used for tuning)" : "Held-out pages"));
        tb.appendChild(sr); lastSplit = any.split;
      }
      var tr = el("tr", { class: "clickable", tabindex: "0" });
      tr.appendChild(el("td", null, fx.replace("holdout_", "")));
      data.extractors.forEach(function (ex) {
        var r = byFx[fx][ex], td = el("td", { class: "n" });
        var b = el("span", { class: "bar" }), i = el("i"); i.style.width = (r.f1 * 100) + "%"; b.appendChild(i);
        td.append(b, el("span", { class: cls(r.f1) }, r.f1.toFixed(3)));
        tr.appendChild(td);
      });
      tr.appendChild(el("td", { class: "n" }, any.gold_rows + " · " + any.pages + "p"));
      var show = function () { renderFields(fx, byFx[fx]); };
      tr.addEventListener("click", show);
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter") show(); });
      tb.appendChild(tr);
    });
    t.appendChild(tb);
  }
  function renderFields(fx, rows) {
    $("fieldsTitle").textContent = "Per-field F1: " + fx.replace("holdout_", "held-out ");
    var box = $("fields"); box.innerHTML = "";
    var names = Object.keys(rows[data.extractors[0]].per_field);
    var t = el("table"), hr = el("tr"); hr.appendChild(el("th", null, "Field"));
    data.extractors.forEach(function (ex) { hr.appendChild(el("th", { class: "n" }, ex)); });
    var th = el("thead"); th.appendChild(hr); t.appendChild(th);
    var tb = el("tbody");
    names.forEach(function (n) {
      var tr = el("tr"); tr.appendChild(el("td", { class: "mono" }, n));
      data.extractors.forEach(function (ex) { var f = rows[ex].per_field[n].f1; tr.appendChild(el("td", { class: "n" })).appendChild(el("span", { class: cls(f) }, f.toFixed(3))); });
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    var w = el("div", { class: "table-wrap" }); w.style.width = "100%"; w.appendChild(t); box.appendChild(w);
  }
  function renderAll() { renderKpis(); renderMatrix(); }

  document.addEventListener("DOMContentLoaded", function () {
    $("splitSeg").addEventListener("click", function (e) {
      var b = e.target.closest("button"); if (!b || !data) return;
      split = b.dataset.split;
      $("splitSeg").querySelectorAll("button").forEach(function (o) { o.setAttribute("aria-pressed", String(o === b)); });
      renderAll();
    });
    fetch("/health").then(function (r) { return r.json(); }).then(function (h) {
      $("claudeText").textContent = h.claude ? "Claude on" : "offline";
      $("claudeBadge").className = "badge " + (h.claude ? "" : "neutral");
    }).catch(function () {});
    fetch("/api/eval").then(function (r) { return r.json(); }).then(function (d) {
      data = d; renderAll();
      var secs = d.results.reduce(function (a, r) { return a + r.seconds; }, 0);
      var pages = d.results.reduce(function (a, r) { return a + r.pages; }, 0);
      $("foot").textContent = d.results.length + " runs over " + pages + " pages in " + secs.toFixed(2) + " s on this server (" + (pages / secs).toFixed(0) + " pages/s, offline).";
    }).catch(function (e) { $("foot").textContent = "Could not load the evaluation: " + e.message; });
  });
})();
