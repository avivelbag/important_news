(function () {
  var section = document.getElementById("personalized-feed");
  var global = document.getElementById("global-feed");
  var list = document.getElementById("personalized-list");
  var empty = document.getElementById("personalized-empty");
  var toggle = document.getElementById("toggle-feed");
  if (!section || !global || !list || !toggle) return;
  var showingGlobal = false;
  var algorithm = "balanced";

  function escapeText(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function render(stories) {
    list.innerHTML = "";
    if (!stories || !stories.length) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    for (var i = 0; i < stories.length; i++) {
      var s = stories[i];
      var li = document.createElement("li");
      li.className = "story";
      li.setAttribute("data-story-id", s.id);
      li.innerHTML =
        '<a class="title" href="' + escapeText(s.url) + '">' +
        escapeText(s.title) +
        '</a> <span class="meta">' +
        escapeText(s.source_name || "") + ' &middot; ' +
        escapeText(s.vote_count) + ' points</span>';
      list.appendChild(li);
    }
  }

  function markActive() {
    var buttons = section.querySelectorAll("button.algo");
    for (var i = 0; i < buttons.length; i++) {
      var on = buttons[i].getAttribute("data-algo") === algorithm;
      buttons[i].classList.toggle("active", on);
    }
  }

  function load() {
    fetch("/api/user/feed?algorithm=" + encodeURIComponent(algorithm) +
      "&limit=20", { credentials: "same-origin" })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        if (!data || data.user_id == null) return;
        section.hidden = false;
        if (!showingGlobal) global.hidden = true;
        algorithm = data.algorithm || algorithm;
        markActive();
        render(data.stories);
      })
      .catch(function () {});
  }

  function pickAlgorithm(value) {
    algorithm = value;
    markActive();
    fetch("/api/user/preferences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ algorithm: value })
    }).catch(function () {});
    load();
  }

  function toggleView() {
    showingGlobal = !showingGlobal;
    global.hidden = !showingGlobal;
    list.hidden = showingGlobal;
    empty.hidden = showingGlobal || empty.hidden;
    toggle.textContent =
      showingGlobal ? "Back to your feed" : "View all stories";
  }

  function init() {
    var buttons = section.querySelectorAll("button.algo");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", function () {
        pickAlgorithm(this.getAttribute("data-algo"));
      });
    }
    toggle.addEventListener("click", toggleView);
    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
