(function () {
  var STORAGE_KEY = "voted-stories";

  function readVotes() {
    try {
      return JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}");
    } catch (e) {
      return {};
    }
  }

  function writeVotes(state) {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (e) {}
  }

  function mark(li, value) {
    var up = li.querySelector("button.vote.up");
    var down = li.querySelector("button.vote.down");
    if (up) up.classList.toggle("voted", value === 1);
    if (down) down.classList.toggle("voted", value === -1);
  }

  function send(li, value) {
    var id = li.getAttribute("data-story-id");
    if (!id) return;
    fetch("/api/vote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ story_id: Number(id), vote_value: value })
    })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        if (!data) return;
        var points = li.querySelector(".points");
        if (points) points.textContent = data.points + " points";
        var down = li.querySelector(".downvotes");
        if (down) down.textContent = data.downvotes + " downvotes";
        var state = readVotes();
        if (value === 0) { delete state[id]; } else { state[id] = value; }
        writeVotes(state);
        mark(li, value);
      })
      .catch(function () {});
  }

  function click(button) {
    var li = button.closest("li.story");
    if (!li) return;
    var id = li.getAttribute("data-story-id");
    var current = readVotes()[id] || 0;
    var value = button.classList.contains("up") ? 1 : -1;
    if (current === value) value = 0;
    send(li, value);
  }

  function init() {
    var state = readVotes();
    var items = document.querySelectorAll("li.story");
    for (var i = 0; i < items.length; i++) {
      var id = items[i].getAttribute("data-story-id");
      if (id && state[id]) mark(items[i], state[id]);
    }
    var buttons = document.querySelectorAll("button.vote");
    for (var j = 0; j < buttons.length; j++) {
      buttons[j].addEventListener("click", function () { click(this); });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
