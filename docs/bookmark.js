(function () {
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function setToggle(button, bookmarked, count) {
    button.setAttribute("aria-pressed", bookmarked ? "true" : "false");
    button.classList.toggle("saved", !!bookmarked);
    var star = bookmarked ? "\u2605" : "\u2606";
    var c = button.querySelector(".bookmark-count");
    button.textContent = star + " ";
    var span = document.createElement("span");
    span.className = "bookmark-count";
    span.textContent = count == null ? (c ? c.textContent : "0") : count;
    button.appendChild(span);
  }

  function toggle(button) {
    var li = button.closest("li.story");
    var id = li && li.getAttribute("data-story-id");
    if (!id) return;
    fetch("/api/articles/" + Number(id) + "/bookmark", {
      method: "POST",
      credentials: "same-origin"
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        setToggle(button, data.bookmarked, data.bookmark_count);
      })
      .catch(function () {});
  }

  function initToggles() {
    var buttons = document.querySelectorAll("button.bookmark-toggle");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", function () { toggle(this); });
    }
  }

  var listRoot = document.getElementById("bookmarks");

  function renderList(data) {
    var items = (data && data.items) || [];
    if (!items.length) {
      listRoot.innerHTML = '<li class="muted">No saved stories yet.</li>';
      return;
    }
    var html = '';
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      html += '<li class="bookmark" data-story-id="' + esc(it.story_id) +
        '"><label><input type="checkbox" class="bookmark-select"> ' +
        '</label><a class="title" href="' + esc(it.url) + '">' +
        esc(it.title) + '</a> <span class="meta">Bookmarked on ' +
        esc(it.created_at || '') + ' &middot; ' + esc(it.topic) +
        '</span> <button type="button" class="bookmark-remove">Remove</button></li>';
    }
    listRoot.innerHTML = html;
    bindListActions();
  }

  function load() {
    var cat = document.getElementById("bookmark-category");
    var url = "/api/user/bookmarks";
    if (cat && cat.value) url += '?category=' + encodeURIComponent(cat.value);
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data) renderList(data); })
      .catch(function () {
        listRoot.innerHTML = '<li class="muted">Could not load bookmarks.</li>';
      });
  }

  function removeOne(id) {
    fetch("/api/articles/" + Number(id) + "/bookmark", {
      method: "DELETE",
      credentials: "same-origin"
    }).then(function () { load(); }).catch(function () {});
  }

  function bulkDelete() {
    var boxes = listRoot.querySelectorAll(".bookmark-select:checked");
    var ids = [];
    for (var i = 0; i < boxes.length; i++) {
      var li = boxes[i].closest("li.bookmark");
      if (li) ids.push(Number(li.getAttribute("data-story-id")));
    }
    if (!ids.length) return;
    fetch("/api/user/bookmarks/bulk-delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ story_ids: ids })
    }).then(function () { load(); }).catch(function () {});
  }

  function bindListActions() {
    var removes = listRoot.querySelectorAll(".bookmark-remove");
    for (var i = 0; i < removes.length; i++) {
      removes[i].addEventListener('click', function () {
        var li = this.closest("li.bookmark");
        if (li) removeOne(li.getAttribute("data-story-id"));
      });
    }
  }

  function init() {
    initToggles();
    if (listRoot) {
      var cat = document.getElementById("bookmark-category");
      if (cat) cat.addEventListener("change", load);
      var bulk = document.getElementById("bookmark-bulk-delete");
      if (bulk) bulk.addEventListener("click", bulkDelete);
      load();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
