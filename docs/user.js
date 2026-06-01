(function () {
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  function param(name) {
    var m = new RegExp('[?&]' + name + '=([^&]*)').exec(
      window.location.search);
    return m ? decodeURIComponent(m[1].replace(/\+/g, " ")) : "";
  }

  var root = document.getElementById("profile");
  var user = param('u');
  if (!root) return;
  if (!user) {
    root.innerHTML = '<p class="muted">No user specified.</p>';
    return;
  }

  var state = { tab: 'articles', pages: { articles: 1, comments: 1 } };

  function renderActivity(panel, data, kind) {
    var html = '';
    var items = (data && data.items) || [];
    if (!items.length) {
      html = '<p class="muted">Nothing here yet.</p>';
    } else {
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        if (kind === 'articles') {
          html += '<div class="activity-item">' +
            '<a class="title" href="' + esc(it.url) + '">' +
            esc(it.title) + '</a> <span class="activity-kind">' +
            esc(it.activity) + ' &middot; ' + esc(it.timestamp || '') +
            '</span></div>';
        } else {
          html += '<div class="activity-item">' + esc(it.body) +
            ' <span class="activity-kind">' + esc(it.vote_count) +
            ' points &middot; ' + esc(it.timestamp || '') +
            '</span></div>';
        }
      }
    }
    var total = (data && data.total) || 0;
    var perPage = (data && data.per_page) || 20;
    var page = state.pages[kind];
    var hasNext = page * perPage < total;
    html += '<div class="pager">' +
      '<button type="button" class="page-btn" data-dir="-1"' +
      (page <= 1 ? ' disabled' : '') + '>Prev</button>' +
      '<span>Page ' + page + '</span>' +
      '<button type="button" class="page-btn" data-dir="1"' +
      (hasNext ? '' : ' disabled') + '>Next</button></div>';
    panel.innerHTML = html;
    var btns = panel.querySelectorAll('button.page-btn');
    for (var j = 0; j < btns.length; j++) {
      btns[j].addEventListener('click', function () {
        if (this.disabled) return;
        state.pages[kind] += Number(this.getAttribute('data-dir'));
        if (state.pages[kind] < 1) state.pages[kind] = 1;
        loadActivity(panel, kind);
      });
    }
  }

  function loadActivity(panel, kind) {
    var url = '/api/users/' + encodeURIComponent(user) + '/' + kind +
      '?page=' + state.pages[kind];
    fetch(url)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data) renderActivity(panel, data, kind);
      })
      .catch(function () {});
  }

  function renderProfile(p) {
    if (p.is_private) {
      root.innerHTML = '<div class="profile-header"><h2>' +
        esc(p.username) + '</h2><p class="private-note">' +
        'This profile is private.</p></div>';
      return;
    }
    root.innerHTML = '<div class="profile-header">' +
      '<h2>' + esc(p.username) + '</h2>' +
      '<div class="profile-karma">' + esc(p.karma) + ' karma</div>' +
      (p.bio ? '<p class="profile-bio">' + esc(p.bio) + '</p>' : '') +
      '<div class="profile-stats">' + esc(p.submission_count) +
      ' submitted &middot; ' + esc(p.vote_count) + ' votes &middot; ' +
      esc(p.comment_count) + ' comments</div></div>' +
      '<div class="profile-tabs">' +
      '<button type="button" class="tab active" data-tab="articles">' +
      'Articles</button>' +
      '<button type="button" class="tab" data-tab="comments">' +
      'Comments</button></div>' +
      '<div id="activity"></div>';
    var panel = document.getElementById('activity');
    var tabs = root.querySelectorAll('button.tab');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener('click', function () {
        var kind = this.getAttribute('data-tab');
        state.tab = kind;
        for (var k = 0; k < tabs.length; k++) {
          tabs[k].classList.toggle('active',
            tabs[k].getAttribute('data-tab') === kind);
        }
        loadActivity(panel, kind);
      });
    }
    loadActivity(panel, 'articles');
  }

  fetch('/api/users/' + encodeURIComponent(user))
    .then(function (r) {
      if (r.status === 404) return { _missing: true };
      return r.ok ? r.json() : null;
    })
    .then(function (p) {
      if (!p) { root.innerHTML = '<p class="muted">Could not load profile.</p>'; return; }
      if (p._missing) { root.innerHTML = '<p class="muted">User not found.</p>'; return; }
      renderProfile(p);
    })
    .catch(function () {
      root.innerHTML = '<p class="muted">Could not load profile.</p>';
    });
})();
