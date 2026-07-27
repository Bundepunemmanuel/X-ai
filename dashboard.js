const BADGE_LABELS = {
  question: "Clarifying question",
  mention: "Kairo mention",
  followup: "Follow-up reply",
  dm: "DM reply",
  original_post: "Original post",
};

let currentTab = "feed";

// ─── Tabs ────────────────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    currentTab = tab.dataset.tab;
    document.getElementById("feed-view").classList.toggle("hidden", currentTab !== "feed");
    document.getElementById("chat-view").classList.toggle("hidden", currentTab !== "chat");
    document.getElementById("session-view").classList.toggle("hidden", currentTab !== "session");
    if (currentTab === "chat") loadChatHistory();
  });
});

document.getElementById("session-import-btn").addEventListener("click", async () => {
  const textarea = document.getElementById("session-textarea");
  const resultDiv = document.getElementById("session-result");
  const value = textarea.value.trim();
  if (!value) return;

  resultDiv.textContent = "Importing...";
  resultDiv.style.color = "#8892A0";

  try {
    const res = await fetch("/api/session/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_json: value }),
    });
    const data = await res.json();
    resultDiv.textContent = data.message;
    resultDiv.style.color = data.success ? "#7DD3C0" : "#C0736B";

    const jsonBox = document.getElementById("session-json-box");
    const jsonOutput = document.getElementById("session-json-output");
    if (data.session_json) {
      jsonOutput.value = data.session_json;
      jsonBox.classList.remove("hidden");
    }

    if (data.success) {
      textarea.value = "";
      setTimeout(loadFeed, 1000);
    }
  } catch (e) {
    resultDiv.textContent = "Request failed: " + e.message;
    resultDiv.style.color = "#C0736B";
  }
});

// ─── Active/paused toggle ────────────────────────────────────────────────
let isActive = true;
document.getElementById("active-toggle").addEventListener("click", async () => {
  isActive = !isActive;
  await fetch("/api/settings/active", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active: isActive }),
  });
  renderActiveState();
});

function renderActiveState() {
  const pill = document.getElementById("active-toggle");
  const label = document.getElementById("active-label");
  pill.classList.toggle("paused", !isActive);
  label.textContent = isActive ? "Active" : "Paused";
}

// ─── Feed ────────────────────────────────────────────────────────────────
async function loadFeed() {
  try {
    const res = await fetch("/api/feed");
    const data = await res.json();

    isActive = data.active;
    renderActiveState();

    document.getElementById("stat-pending").textContent = data.drafts.length;
    document.getElementById("stat-posted").textContent = data.counts.replies_posted || 0;
    const skipped = data.recent_activity.filter((a) => a.message.includes("Skipped")).length;
    document.getElementById("stat-skipped").textContent = skipped;

    document.getElementById("x-login-warning").classList.toggle("hidden", data.x_logged_in);

    const list = document.getElementById("feed-list");
    if (data.drafts.length === 0) {
      list.innerHTML = '<div class="empty-state">No drafts waiting right now. It\'ll surface new ones here as it finds good threads.</div>';
      return;
    }

    list.innerHTML = data.drafts.map(renderCard).join("");
    attachCardHandlers();
  } catch (e) {
    console.error("Failed to load feed", e);
  }
}

function renderCard(draft) {
  const badgeType = draft.draft_type;
  const badgeLabel = BADGE_LABELS[badgeType] || badgeType;
  const initial = (draft.author_name || "?").charAt(0).toUpperCase();

  return `
    <div class="card" data-id="${draft.id}">
      <div class="card-rail ${badgeType}"></div>
      <div class="card-body">
        <div class="card-header">
          <div class="avatar">${initial}</div>
          <div>
            <div class="name">${escapeHtml(draft.author_name || "")}</div>
            <div class="handle">${escapeHtml(draft.author_handle || "")}</div>
          </div>
        </div>
        ${draft.original_post ? `<p class="post-text">${escapeHtml(draft.original_post)}</p>` : ""}
        ${draft.context_snippet ? `<p class="context-text">${escapeHtml(draft.context_snippet)}</p>` : ""}
        ${draft.pain_quote ? `<div class="pain-quote">"${escapeHtml(draft.pain_quote)}"</div>` : ""}
        <div class="badge ${badgeType}">${badgeLabel}</div>
        <textarea class="draft-text" rows="3">${escapeHtml(draft.draft_text)}</textarea>
        <div class="card-actions">
          <button class="btn-approve" data-action="approve">Approve</button>
          <button class="btn-icon" data-action="edit-save" style="display:none">Save</button>
          <button class="btn-icon btn-skip" data-action="skip">Skip</button>
        </div>
      </div>
    </div>
  `;
}

function attachCardHandlers() {
  document.querySelectorAll(".card").forEach((card) => {
    const id = card.dataset.id;
    const textarea = card.querySelector(".draft-text");

    card.querySelector('[data-action="approve"]').addEventListener("click", async () => {
      const editedText = textarea.value;
      const res = await fetch(`/api/drafts/${id}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ edited_text: editedText }),
      });
      const result = await res.json();
      if (result.success) {
        replaceWithDoneCard(card, "approved");
      } else {
        alert(result.error || "Could not post right now — pacing cap may be reached.");
      }
    });

    card.querySelector('[data-action="skip"]').addEventListener("click", async () => {
      await fetch(`/api/drafts/${id}/skip`, { method: "POST" });
      replaceWithDoneCard(card, "skipped");
    });
  });
}

function replaceWithDoneCard(card, status) {
  const handle = card.querySelector(".handle").textContent;
  const label = status === "approved" ? `Posted to ${handle}` : `Skipped — ${handle}`;
  const el = document.createElement("div");
  el.className = `done-card ${status}`;
  el.innerHTML = `<span>${label}</span>`;
  card.replaceWith(el);
  setTimeout(loadFeed, 1500);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str || "";
  return div.innerHTML;
}

// ─── Chat ────────────────────────────────────────────────────────────────
async function loadChatHistory() {
  const res = await fetch("/api/chat/history");
  const data = await res.json();
  const container = document.getElementById("chat-messages");
  container.innerHTML = data.messages.map(
    (m) => `<div class="chat-msg ${m.role}">${escapeHtml(m.content)}</div>`
  ).join("");
  container.scrollTop = container.scrollHeight;
}

document.getElementById("chat-send").addEventListener("click", sendChatMessage);
document.getElementById("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChatMessage();
});

async function sendChatMessage() {
  const input = document.getElementById("chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";

  const container = document.getElementById("chat-messages");
  container.innerHTML += `<div class="chat-msg user">${escapeHtml(message)}</div>`;
  container.scrollTop = container.scrollHeight;

  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  const data = await res.json();
  container.innerHTML += `<div class="chat-msg assistant">${escapeHtml(data.reply)}</div>`;
  container.scrollTop = container.scrollHeight;
}

document.getElementById("session-copy-btn").addEventListener("click", async () => {
  const output = document.getElementById("session-json-output");
  try {
    await navigator.clipboard.writeText(output.value);
    const btn = document.getElementById("session-copy-btn");
    const original = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(() => { btn.textContent = original; }, 1500);
  } catch (e) {
    output.select();
    document.execCommand("copy");
  }
});

// ─── Polling ─────────────────────────────────────────────────────────────
loadFeed();
setInterval(loadFeed, 8000);
