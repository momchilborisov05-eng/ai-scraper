// Extraction studio: builds the request, renders rows, the outlined preview, learned rules and the page log.
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var TYPES = ["string", "number", "integer", "boolean", "url"];
  var state = { fixtures: [], claude: false, fetch: false, source: "fixture", mode: "auto", last: null };

  var HINTS = {
    auto: "Induction when example rows are given, otherwise Hybrid if Claude is available, otherwise Heuristic.",
    heuristic: "Zero-shot: finds the repeated records and matches columns by name, type and nearby words. Offline.",
    induction: "Learns exact rules (path, attribute, number index) from your 1-2 example rows. Offline, the most accurate.",
    claude: "Every page goes to Claude as cleaned text with a JSON schema; rows are validated and repaired.",
    hybrid: "Claude reads page 1, a wrapper is learned from its rows and verified; later pages run offline."
  };

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text != null) e.textContent = text;
    return e;
  }
  function showError(msg) { var b = $("error"); b.textContent = msg; b.hidden = !msg; }
  function api(path, body) {
    return fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {})
      .then(function (r) {
        return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || r.statusText); return d; });
      });
  }

  // ---------- columns editor ----------
  function addCol(f) {
    f = f || { name: "", type: "string", description: "" };
    var row = el("div", { class: "col" });
    var name = el("input", { class: "field-input name", placeholder: "column_name", "aria-label": "Column name", title: f.description || "" });
    name.value = f.name;
    name.dataset.desc = f.description || "";
    var type = el("select", { class: "field-input", "aria-label": "Column type" });
    TYPES.forEach(function (t) { var o = el("option", { value: t }, t); if (t === f.type) o.selected = true; type.appendChild(o); });
    var x = el("button", { class: "x", type: "button", "aria-label": "Remove column" });
    x.innerHTML = '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>';
    x.addEventListener("click", function () { row.remove(); });
    row.append(name, type, x);
    $("cols").appendChild(row);
  }
  function setSchema(schema) { $("cols").innerHTML = ""; schema.fields.forEach(addCol); }
  function readSchema() {
    var fields = [];
    $("cols").querySelectorAll(".col").forEach(function (r) {
      var n = r.querySelector("input").value.trim();
      if (n) fields.push({ name: n, type: r.querySelector("select").value, description: r.querySelector("input").dataset.desc || n });
    });
    return { fields: fields, description: $("request").value.trim() };
  }

  // ---------- form state ----------
  function currentFixture() { return state.fixtures.filter(function (f) { return f.name === $("fixture").value; })[0]; }
  function loadFixture() {
    var f = currentFixture();
    if (!f) return;
    $("request").value = f.request;
    setSchema(f.schema);
    $("examples").value = "";
    $("pages").value = f.pages;
    $("fixtureNote").innerHTML = "";
    $("fixtureNote").append(el("b", null, f.records + " hand-checked rows, " + f.pages + " page" + (f.pages > 1 ? "s" : "") + ". "),
      document.createTextNode(f.notes || ""));
  }
  function setSeg(seg, attr, value) {
    $(seg).querySelectorAll("button").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset[attr] === value)); });
  }
  function setSource(src) {
    state.source = src;
    setSeg("sourceSeg", "src", src);
    $("srcFixture").hidden = src !== "fixture";
    $("srcHtml").hidden = src !== "html";
    $("srcUrl").hidden = src !== "url";
    $("fillExamples").hidden = src !== "fixture";
  }
  function setMode(m) { state.mode = m; setSeg("modeSeg", "mode", m); $("modeHint").textContent = HINTS[m]; }

  // ---------- rendering ----------
  function fmtCell(td, v, type) {
    if (v === null || v === undefined || v === "") { td.className = "null"; td.textContent = "null"; return; }
    if (type === "boolean") { td.className = v ? "bool-yes" : "bool-no"; td.textContent = v ? "yes" : "no"; return; }
    if (type === "number" || type === "integer") { td.className = "n"; td.textContent = Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }); return; }
    if (type === "url") { var s = el("span", { class: "u" }, String(v).replace(/^https?:\/\//, "")); td.title = v; td.appendChild(s); return; }
    td.textContent = v; td.title = v;
  }
  function renderTable(d) {
    var t = $("table"); t.innerHTML = "";
    var thead = el("thead"), hr = el("tr");
    hr.appendChild(el("th", { class: "n" }, "#"));
    d.schema.fields.forEach(function (f) { hr.appendChild(el("th", { class: (f.type === "number" || f.type === "integer") ? "n" : "" }, f.name)); });
    thead.appendChild(hr); t.appendChild(thead);
    var tb = el("tbody");
    d.rows.forEach(function (r, i) {
      var tr = el("tr"); tr.appendChild(el("td", { class: "n muted" }, String(i + 1)));
      d.schema.fields.forEach(function (f) { var td = el("td"); fmtCell(td, r[f.name], f.type); tr.appendChild(td); });
      tb.appendChild(tr);
    });
    if (!d.rows.length) { var tr = el("tr"), td = el("td", { colspan: String(d.schema.fields.length + 1), class: "null" }, "No rows."); tr.appendChild(td); tb.appendChild(tr); }
    t.appendChild(tb);
  }
  function renderPages(d) {
    var t = $("pagesTable"); t.innerHTML = "";
    var hr = el("tr");
    ["#", "Method", "Rows", "Records seen", "ms", "Page tokens (est.)", "Text tokens (est.)", "Next page via"].forEach(function (h, i) {
      hr.appendChild(el("th", { class: i === 1 || i === 7 ? "" : "n" }, h));
    });
    var th = el("thead"); th.appendChild(hr); t.appendChild(th);
    var tb = el("tbody");
    d.pages.forEach(function (p, i) {
      var tr = el("tr");
      tr.appendChild(el("td", { class: "n" }, String(i + 1)));
      var m = el("td"); m.appendChild(el("span", { class: "method" + (p.method === "claude" ? " claude" : "") + (p.error ? " err" : "") }, p.error ? "error" : p.method)); tr.appendChild(m);
      [p.rows, p.records_found, p.ms, p.html_tokens_est, p.text_tokens_est].forEach(function (v) { tr.appendChild(el("td", { class: "n" }, Number(v).toLocaleString())); });
      tr.appendChild(el("td", { title: p.error || p.next_url || "" }, p.error ? p.error : (p.next_url ? p.next_rule : "none (last page)")));
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    var ul = $("notes"); ul.innerHTML = "";
    (d.notes || []).forEach(function (n) { ul.appendChild(el("li", null, n)); });
  }
  function renderKpis(d) {
    $("kpis").hidden = false;
    $("kRows").textContent = d.rows.length;
    $("kRowsSub").textContent = d.schema.fields.length + " columns";
    $("kPages").textContent = d.pages.length;
    var methods = {}; d.pages.forEach(function (p) { methods[p.method] = (methods[p.method] || 0) + 1; });
    $("kPagesSub").textContent = Object.keys(methods).map(function (k) { return methods[k] + " " + k; }).join(", ");
    var f1 = $("kF1");
    if (d.score) {
      f1.textContent = d.score.f1.toFixed(3);
      f1.className = "value " + (d.score.f1 >= 0.95 ? "good" : "warn");
      $("kF1Sub").textContent = "P " + d.score.precision.toFixed(2) + " · R " + d.score.recall.toFixed(2) + " vs hand-checked rows";
    } else { f1.textContent = "n/a"; f1.className = "value"; $("kF1Sub").textContent = "no ground truth for this page"; }
    $("kTime").textContent = (d.seconds * 1000).toFixed(0) + " ms";
    $("kTimeSub").textContent = (d.pages.length / Math.max(d.seconds, 1e-6)).toFixed(1) + " pages/s";
    var u = d.usage || {};
    $("kCost").textContent = u.requests ? "$" + u.cost_usd.toFixed(4) : "$0";
    $("kCostSub").textContent = u.requests ? (u.requests + " calls · " + (u.input_tokens + u.output_tokens).toLocaleString() + " tokens · " + u.cache_hits + " cache hits")
      : "no API calls";
  }
  function render(d) {
    state.last = d;
    $("placeholder").hidden = true;
    $("output").hidden = false;
    renderKpis(d); renderTable(d); renderPages(d);
    $("preview").srcdoc = d.preview_html || "";
    $("wrapper").textContent = d.wrapper ? JSON.stringify(d.wrapper, null, 2) : "No wrapper: every page was read by Claude.";
  }

  // ---------- actions ----------
  function run(ev) {
    ev.preventDefault();
    showError("");
    var body = { mode: state.mode, schema: readSchema(), pages: Number($("pages").value) || 1 };
    if (!body.schema.fields.length) { showError("Add at least one column."); return; }
    var ex = $("examples").value.trim();
    if (ex) {
      try { body.examples = JSON.parse(ex); if (!Array.isArray(body.examples)) body.examples = [body.examples]; }
      catch (e) { showError("Example rows must be JSON, e.g. [{\"title\": \"...\", \"price\": 12.5}]"); return; }
    }
    if (state.source === "fixture") body.fixture = $("fixture").value;
    else if (state.source === "html") { body.html = $("html").value; body.base_url = $("baseUrl").value.trim() || undefined; }
    else body.url = $("url").value.trim();
    var btn = $("runBtn"); btn.disabled = true; btn.querySelector("span").textContent = "Extracting...";
    api("/api/extract", body).then(render).catch(function (e) { showError(e.message); })
      .finally(function () { btn.disabled = false; btn.querySelector("span").textContent = "Extract"; });
  }
  function download(fmt) {
    if (!state.last) return;
    fetch("/api/export", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format: fmt, rows: state.last.rows, schema: state.last.schema, filename: state.source === "fixture" ? $("fixture").value : "extract" }) })
      .then(function (r) { if (!r.ok) throw new Error("export failed"); return r.blob(); })
      .then(function (b) {
        var a = el("a", { href: URL.createObjectURL(b), download: (state.source === "fixture" ? $("fixture").value : "extract") + "." + fmt });
        document.body.appendChild(a); a.click(); a.remove();
      }).catch(function (e) { showError(e.message); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("sourceSeg").addEventListener("click", function (e) { var b = e.target.closest("button"); if (b) setSource(b.dataset.src); });
    $("modeSeg").addEventListener("click", function (e) { var b = e.target.closest("button"); if (b && !b.disabled) setMode(b.dataset.mode); });
    $("fixture").addEventListener("change", loadFixture);
    $("addCol").addEventListener("click", function () { addCol(); });
    $("suggestBtn").addEventListener("click", function () {
      var req = $("request").value.trim();
      if (!req) { showError("Describe the columns first, e.g. title, price, rating"); return; }
      api("/api/schema", { request: req, use_claude: state.claude }).then(function (d) { setSchema(d.schema); showError(""); })
        .catch(function (e) { showError(e.message); });
    });
    $("fillExamples").addEventListener("click", function () {
      var f = currentFixture(); if (f) $("examples").value = JSON.stringify(f.examples, null, 1);
    });
    document.querySelectorAll(".tabs [role=tab]").forEach(function (t) {
      t.addEventListener("click", function () {
        document.querySelectorAll(".tabs [role=tab]").forEach(function (o) { o.setAttribute("aria-selected", String(o === t)); });
        document.querySelectorAll("[data-panel]").forEach(function (p) { p.hidden = p.dataset.panel !== t.dataset.tab; });
      });
    });
    document.querySelectorAll("[data-fmt]").forEach(function (b) { b.addEventListener("click", function () { download(b.dataset.fmt); }); });
    $("form").addEventListener("submit", run);
    setMode("auto");

    api("/api/fixtures").then(function (d) {
      state.fixtures = d.fixtures; state.claude = d.claude; state.fetch = d.fetch;
      d.fixtures.forEach(function (f) {
        $("fixture").appendChild(el("option", { value: f.name }, f.name.replace("holdout_", "held-out: ") + "  (" + f.records + " rows)"));
      });
      $("claudeText").textContent = d.claude ? "Claude on" : "offline";
      $("claudeBadge").className = "badge " + (d.claude ? "" : "neutral");
      document.querySelectorAll(".needs-claude").forEach(function (b) {
        b.disabled = !d.claude; if (!d.claude) b.title = "Set ANTHROPIC_API_KEY on the server to enable";
      });
      $("urlNote").textContent = d.fetch ? "Fetched politely (robots.txt, 1 s delay); private addresses are blocked."
        : "URL fetching is off on this server (SSRF protection). Paste the HTML, or run locally with AISCRAPER_ALLOW_FETCH=1.";
      var start = new URLSearchParams(location.search).get("fixture");
      if (start) $("fixture").value = start;
      loadFixture();
      if (new URLSearchParams(location.search).get("run") === "1") {
        if (new URLSearchParams(location.search).get("examples") === "1") $("fillExamples").click();
        var m = new URLSearchParams(location.search).get("mode"); if (m && HINTS[m]) setMode(m);
        $("form").requestSubmit();
      }
    }).catch(function (e) { showError("Could not load the sample pages: " + e.message); });
  });
})();
