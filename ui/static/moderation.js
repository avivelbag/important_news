"use strict";

// Moderation dashboard + public flag-submission helpers.
//
// Admin actions are gated behind the X-Admin-Token header (the same shared
// secret used by the submission queue). The token is cached in sessionStorage
// and cleared on any 403 so a stale value never silently re-fails every click.

var FLAG_REASONS = [
  "spam",
  "off_topic",
  "abuse",
  "misinformation",
  "duplicate",
  "other",
];

function adminToken() {
  var token = sessionStorage.getItem("adminToken");
  if (!token) {
    token = window.prompt("Admin token:");
    if (token) sessionStorage.setItem("adminToken", token);
  }
  return token;
}

function clearTokenOn403(status) {
  if (status === 403) sessionStorage.removeItem("adminToken");
}

// Public: let a reader flag a story/comment. Confirms first and posts the
// chosen reason; returns the fetch promise so callers can chain UI updates.
function submitFlag(contentType, contentId) {
  var reason = window.prompt(
    "Reason (" + FLAG_REASONS.join(", ") + "):",
    "spam"
  );
  if (!reason) return Promise.resolve(null);
  if (FLAG_REASONS.indexOf(reason) === -1) {
    window.alert("Unknown reason: " + reason);
    return Promise.resolve(null);
  }
  if (!window.confirm("Flag this " + contentType + " as '" + reason + "'?")) {
    return Promise.resolve(null);
  }
  var path =
    contentType === "story"
      ? "/api/stories/" + contentId + "/flag"
      : "/api/comments/" + contentId + "/flag";
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: reason }),
  }).then(function (r) {
    window.alert(r.ok ? "Flag submitted. Thank you." : "Flag failed (" + r.status + ").");
    return r;
  });
}

function renderReasons(reasonCounts) {
  return Object.keys(reasonCounts || {})
    .sort()
    .map(function (k) {
      return '<span class="reason">' + k + ": " + reasonCounts[k] + "</span>";
    })
    .join("");
}

function modAction(contentType, contentId, action) {
  var token = adminToken();
  if (!token) return;
  var verb = action === "delete" ? "delete-content" : action;
  if (!window.confirm(action + " this " + contentType + " #" + contentId + "?")) return;
  fetch("/api/flags/" + contentType + "/" + contentId + "/" + verb, {
    method: "POST",
    headers: { "X-Admin-Token": token },
  }).then(function (r) {
    if (r.ok) {
      loadQueue();
    } else {
      clearTokenOn403(r.status);
      window.alert("Action failed (" + r.status + ").");
    }
  });
}

function showAudit(contentType, contentId) {
  var token = adminToken();
  if (!token) return;
  fetch("/api/flags/" + contentType + "/" + contentId + "/actions", {
    headers: { "X-Admin-Token": token },
  })
    .then(function (r) {
      if (!r.ok) {
        clearTokenOn403(r.status);
        throw new Error("audit failed (" + r.status + ")");
      }
      return r.json();
    })
    .then(function (rows) {
      var text = rows.length
        ? rows
            .map(function (a) {
              return (
                a.created_at +
                " — " +
                a.action +
                " by " +
                (a.moderator || "system") +
                (a.detail ? " (" + a.detail + ")" : "")
              );
            })
            .join("\n")
        : "No audit history.";
      window.alert(text);
    })
    .catch(function (e) {
      window.alert(String(e.message || e));
    });
}

function renderRow(item) {
  var hiddenTag = item.is_hidden ? ' <span class="hidden-tag">[hidden]</span>' : "";
  var ct = item.content_type;
  var cid = item.content_id;
  return (
    "<tr>" +
    "<td>" + ct + hiddenTag + "</td>" +
    "<td>" + cid + "</td>" +
    "<td>" + (item.title || "") + "</td>" +
    '<td class="count">' + item.flag_count + "</td>" +
    "<td>" + renderReasons(item.reason_counts) + "</td>" +
    "<td>" +
    '<button class="hide" onclick="modAction(\'' + ct + "'," + cid + ",'hide')\">Hide</button>" +
    '<button class="del" onclick="modAction(\'' + ct + "'," + cid + ",'delete')\">Delete</button>" +
    '<button class="dismiss" onclick="modAction(\'' + ct + "'," + cid + ",'dismiss')\">Dismiss</button>" +
    '<button class="audit" onclick="showAudit(\'' + ct + "'," + cid + ")\">Audit</button>" +
    "</td>" +
    "</tr>"
  );
}

function loadQueue() {
  var token = adminToken();
  if (!token) return;
  var type = document.getElementById("filter-type").value;
  var reason = document.getElementById("filter-reason").value;
  var params = [];
  if (type) params.push("content_type=" + encodeURIComponent(type));
  if (reason) params.push("reason=" + encodeURIComponent(reason));
  var url = "/api/flags" + (params.length ? "?" + params.join("&") : "");
  var status = document.getElementById("status");
  status.textContent = "Loading…";
  fetch(url, { headers: { "X-Admin-Token": token } })
    .then(function (r) {
      if (!r.ok) {
        clearTokenOn403(r.status);
        throw new Error("load failed (" + r.status + ")");
      }
      return r.json();
    })
    .then(function (items) {
      var body = document.getElementById("queue");
      body.innerHTML = items.map(renderRow).join("");
      status.textContent = items.length
        ? items.length + " flagged item(s)."
        : "No flagged content.";
    })
    .catch(function (e) {
      status.textContent = String(e.message || e);
    });
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", function () {
    var reload = document.getElementById("reload");
    if (reload) {
      reload.addEventListener("click", loadQueue);
      loadQueue();
    }
  });
}
