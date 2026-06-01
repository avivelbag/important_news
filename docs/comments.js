(function () {
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function renderNode(c) {
    var author = c.deleted ? "[deleted]" : (c.user_id || "anonymous");
    var authorHtml = (!c.deleted && c.user_id)
      ? '<a class="author" href="user.html?u=' +
        encodeURIComponent(c.user_id) + '">' + esc(author) + '</a>'
      : esc(author);
    var opHtml = c.is_op ? ' <span class="comment-op">OP</span>' : '';
    var children = "";
    if (c.replies && c.replies.length) {
      for (var i = 0; i < c.replies.length; i++) {
        children += renderNode(c.replies[i]);
      }
    }
    var reply = c.deleted ? '' :
      '<button type="button" class="comment-reply-toggle">Reply</button>' +
      '<form class="comment-form comment-reply-form" data-parent-id="' +
      esc(c.id) + '" hidden>' +
      '<textarea name="body" placeholder="Reply"></textarea>' +
      '<button type="submit">Post reply</button></form>';
    var up = c.user_vote === 1 ? ' voted' : '';
    var down = c.user_vote === -1 ? ' voted' : '';
    var score = (c.score == null ? c.vote_count : c.score);
    var votes = c.deleted ? '' :
      '<span class="comment-votes">' +
      '<button type="button" class="cvote up' + up +
      '" aria-label="Upvote">&#9650;</button>' +
      '<span class="comment-score">' + esc(score) + ' points</span>' +
      '<button type="button" class="cvote down' + down +
      '" aria-label="Downvote">&#9660;</button></span> &middot; ';
    var collapsed = c.collapsed ? ' collapsed' : '';
    var toggle = c.collapsed ?
      ' <button type="button" class="comment-collapse-toggle">' +
      '[+]</button>' : '';
    return '<div class="comment' + collapsed + '" data-comment-id="' +
      esc(c.id) + '" data-user-vote="' + esc(c.user_vote || 0) + '">' +
      '<div class="comment-meta">' + votes + authorHtml + opHtml +
      ' &middot; ' + esc(c.created_at || '') + toggle + '</div>' +
      '<div class="comment-content">' +
      '<div class="comment-body">' + esc(c.body) + '</div>' + reply +
      '<div class="comment-replies">' + children + '</div></div></div>';
  }

  function load(story, panel) {
    var id = story.getAttribute("data-story-id");
    var sort = panel.getAttribute('data-sort') || 'score';
    fetch("/api/articles/" + encodeURIComponent(id) + "/comments?sort=" +
      encodeURIComponent(sort), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (thread) {
        var html = '<div class="comment-sort">Sort: ' +
          '<select class="comment-sort-select">' +
          '<option value="score">Score</option>' +
          '<option value="newest">Newest</option>' +
          '<option value="oldest">Oldest</option></select></div>';
        for (var i = 0; i < thread.length; i++) {
          html += renderNode(thread[i]);
        }
        html += '<form class="comment-form">' +
          '<textarea name="body" placeholder="Add a comment"></textarea>' +
          '<button type="submit">Post</button></form>';
        panel.innerHTML = html;
        var sel = panel.querySelector('.comment-sort-select');
        if (sel) sel.value = sort;
      })
      .catch(function () {});
  }

  function vote(comment, direction) {
    var id = comment.getAttribute('data-comment-id');
    if (!id) return;
    var prev = Number(comment.getAttribute('data-user-vote')) || 0;
    var next = prev === direction ? 0 : direction;
    var scoreEl = comment.querySelector('.comment-score');
    var prevText = scoreEl ? scoreEl.textContent : null;
    // Optimistic: shift the displayed score by the vote delta at once.
    applyVote(comment, next, scoreEl, prev !== 0 || next !== 0 ?
      (next - prev) : 0);
    var path = direction === 1 ? 'upvote' : 'downvote';
    fetch('/api/comments/' + encodeURIComponent(id) + '/' + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin"
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) throw new Error('vote failed');
        comment.setAttribute('data-user-vote', data.user_vote);
        if (scoreEl) scoreEl.textContent = data.score + ' points';
        markVote(comment, data.user_vote);
      })
      .catch(function () {
        // Revert the optimistic change on any error.
        comment.setAttribute('data-user-vote', prev);
        if (scoreEl && prevText != null) scoreEl.textContent = prevText;
        markVote(comment, prev);
      });
  }

  function applyVote(comment, next, scoreEl, delta) {
    comment.setAttribute('data-user-vote', next);
    markVote(comment, next);
    if (scoreEl && delta) {
      var cur = parseInt(scoreEl.textContent, 10);
      if (!isNaN(cur)) scoreEl.textContent = (cur + delta) + ' points';
    }
  }

  function markVote(comment, value) {
    var up = comment.querySelector(':scope > .comment-meta .cvote.up');
    var down = comment.querySelector(':scope > .comment-meta .cvote.down');
    if (up) up.classList.toggle('voted', value === 1);
    if (down) down.classList.toggle('voted', value === -1);
  }

  function submit(story, panel, form) {
    var ta = form.querySelector("textarea");
    var body = ta ? ta.value.trim() : '';
    if (!body) return;
    var id = story.getAttribute("data-story-id");
    var parent = form.getAttribute("data-parent-id");
    var payload = { story_id: Number(id), body: body };
    if (parent) payload.parent_comment_id = Number(parent);
    fetch("/api/comments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(payload)
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data) load(story, panel); });
  }

  function init() {
    var stories = document.querySelectorAll("li.story");
    for (var i = 0; i < stories.length; i++) {
      (function (story) {
        var toggle = story.querySelector(".comments-toggle");
        var panel = story.querySelector(".comments");
        if (!toggle || !panel) return;
        var loaded = false;
        toggle.addEventListener("click", function () {
          panel.hidden = !panel.hidden;
          if (!panel.hidden && !loaded) { loaded = true; load(story, panel); }
        });
        panel.addEventListener("submit", function (e) {
          if (e.target && e.target.classList.contains("comment-form")) {
            e.preventDefault();
            submit(story, panel, e.target);
          }
        });
        panel.addEventListener("change", function (e) {
          if (e.target &&
              e.target.classList.contains("comment-sort-select")) {
            panel.setAttribute('data-sort', e.target.value);
            load(story, panel);
          }
        });
        panel.addEventListener("click", function (e) {
          var t = e.target;
          if (!t) return;
          if (t.classList.contains("comment-reply-toggle")) {
            var f = t.nextElementSibling;
            if (f) f.hidden = !f.hidden;
          } else if (t.classList.contains("comment-collapse-toggle")) {
            var node = t.closest(".comment");
            if (node) node.classList.toggle("collapsed");
          } else if (t.classList.contains("cvote")) {
            var c = t.closest(".comment");
            if (c) vote(c, t.classList.contains('up') ? 1 : -1);
          }
        });
      })(stories[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
