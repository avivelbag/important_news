(function () {
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  var root = document.getElementById("leaderboard");
  if (!root) return;
  fetch('/api/users/leaderboard')
    .then(function (r) { return r.ok ? r.json() : []; })
    .then(function (rows) {
      if (!rows.length) {
        root.innerHTML = '<li class="muted">No users yet.</li>';
        return;
      }
      var html = '';
      for (var i = 0; i < rows.length; i++) {
        var u = rows[i];
        html += '<li><span class="lb-rank">' + esc(u.rank) +
          '.</span><a class="author" href="user.html?u=' +
          encodeURIComponent(u.username) + '">' + esc(u.username) +
          '</a><span class="lb-karma">' + esc(u.karma) +
          ' karma</span></li>';
      }
      root.innerHTML = html;
    })
    .catch(function () {
      root.innerHTML = '<li class="muted">Could not load leaderboard.</li>';
    });
})();
