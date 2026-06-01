(function () {
  var STORAGE_KEY = "category-filter";
  var VALID = ["all", "ai", "aerospace"];

  function readState() {
    var hash = (window.location.hash || "").replace(/^#/, "");
    if (VALID.indexOf(hash) !== -1) return hash;
    try {
      var stored = window.localStorage.getItem(STORAGE_KEY);
      if (VALID.indexOf(stored) !== -1) return stored;
    } catch (e) {}
    return "all";
  }

  function matches(topic, filter) {
    if (filter === "all") return true;
    if (topic === filter) return true;
    if (topic === "both") return filter === "ai" || filter === "aerospace";
    return false;
  }

  function apply(filter) {
    var sections = document.querySelectorAll("section.topic");
    for (var i = 0; i < sections.length; i++) {
      var topic = sections[i].getAttribute("data-topic");
      sections[i].hidden = !matches(topic, filter);
    }
    var buttons = document.querySelectorAll("button.filter");
    for (var j = 0; j < buttons.length; j++) {
      var f = buttons[j].getAttribute("data-filter");
      buttons[j].classList.toggle("active", f === filter);
    }
  }

  function setState(filter) {
    try {
      window.localStorage.setItem(STORAGE_KEY, filter);
    } catch (e) {}
    if (window.location.hash.replace(/^#/, "") !== filter) {
      window.location.hash = filter;
    }
    apply(filter);
  }

  function init() {
    var buttons = document.querySelectorAll("button.filter");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", function () {
        setState(this.getAttribute("data-filter"));
      });
    }
    window.addEventListener("hashchange", function () {
      apply(readState());
    });
    apply(readState());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
