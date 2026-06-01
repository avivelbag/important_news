(function () {
  var DEBOUNCE_MS = 300;
  var box = document.getElementById("search-box");
  var panel = document.getElementById("search-results");
  if (!box || !panel) return;
  var timer = null;

  function activeCategory() {
    var active = document.querySelector("button.filter.active");
    var f = active ? active.getAttribute("data-filter") : "all";
    return f && f !== "all" ? f : null;
  }

  // Optional advanced-filter controls; absent on pages without them.
  var FILTER_IDS = ["filter-sources", "filter-topics",
    "filter-min-score", "filter-min-comments",
    "filter-date-from", "filter-date-to", "filter-sort"];
  var FILTER_PARAMS = ["sources", "topics", "min_score",
    "min_comments", "date_from", "date_to", "sort"];

  function filterParams() {
    var out = "";
    for (var i = 0; i < FILTER_IDS.length; i++) {
      var el = document.getElementById(FILTER_IDS[i]);
      if (!el) continue;
      var v = el.value.trim();
      if (!v || v === "relevance") continue;
      out += "&" + FILTER_PARAMS[i] + "=" + encodeURIComponent(v);
    }
    return out;
  }

  function syncUrl(query) {
    if (!window.history || !window.history.replaceState) return;
    window.history.replaceState(null, "", "?" + query);
  }

  function render(results) {
    if (!results.length) {
      panel.innerHTML = '<p class="search-empty">No results</p>';
      panel.hidden = false;
      return;
    }
    var html = "";
    for (var i = 0; i < results.length; i++) {
      var r = results[i];
      var a = document.createElement("a");
      a.href = r.url;
      a.textContent = r.title;
      var item = document.createElement("div");
      item.className = "search-result";
      item.appendChild(a);
      html += item.outerHTML;
    }
    panel.innerHTML = html;
    panel.hidden = false;
  }

  function run() {
    var q = box.value.trim();
    if (q.length < 2) {
      panel.hidden = true;
      panel.innerHTML = "";
      return;
    }
    var url = "/api/search?q=" + encodeURIComponent(q);
    var cat = activeCategory();
    if (cat) url += "&category=" + encodeURIComponent(cat);
    url += filterParams();
    syncUrl(url.slice(url.indexOf("?") + 1));
    fetch(url)
      .then(function (resp) { return resp.ok ? resp.json() : []; })
      .then(render)
      .catch(function () { panel.hidden = true; });
  }

  var clear = document.getElementById("filter-clear");
  if (clear) {
    clear.addEventListener("click", function () {
      for (var i = 0; i < FILTER_IDS.length; i++) {
        var el = document.getElementById(FILTER_IDS[i]);
        if (!el) continue;
        if (el.tagName === "SELECT") { el.value = "relevance"; }
        else { el.value = ""; }
      }
      run();
    });
  }
  for (var fi = 0; fi < FILTER_IDS.length; fi++) {
    var fel = document.getElementById(FILTER_IDS[fi]);
    if (fel) fel.addEventListener("change", run);
  }

  box.addEventListener("input", function () {
    if (timer) clearTimeout(timer);
    timer = setTimeout(run, DEBOUNCE_MS);
  });
  box.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      if (timer) clearTimeout(timer);
      run();
    }
  });
})();
