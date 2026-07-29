const BADGE_LABELS = {
  question: "Clarifying question",
  mention: "Kairo mention",
  followup: "Follow-up reply",
  original_post: "Original post",
  knowledge_question: "Needs your input",
};

let currentTab = "feed";
let reviewMode = false;
let reviewIndex = 0;

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    currentTab = tab.dataset.tab;
    document.getElementById("feed-view").classList.toggle("hidden", currentTab !== "feed");
    document.getElementById("chat-view").classList.toggle("hidden", currentTab !== "chat");
    if (currentTab === "chat") loadChatHistory();
  });
});

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

    renderScanBanner(data.scan, data.scan_interval_seconds);

    const list = document.getElementById("feed-list");
    const reviewNav = document.getElementById("review-nav");

    if (data.drafts.length === 0) {
      list.innerHTML = '<div class="empty-state">No drafts waiting right now. It\'ll surface new ones here as it finds good threads.</div>';
      reviewNav.classList.add("hidden");
      return;
    }

    if (reviewMode) {
      if (reviewIndex >= data.drafts.length) reviewIndex = 0;
      const draft = data.drafts[reviewIndex];
      list.innerHTML = renderCard(draft);
      reviewNav.classList.remove("hidden");
      document.getElementById("review-position").textContent = `${reviewIndex + 1} of ${data.drafts.length}`;
    } else {
      list.innerHTML = data.drafts.map(renderCard).join("");
      reviewNav.classList.add("hidden");
    }
    attachCardHandlers();
  } catch (e) {
    console.error("Failed to load feed", e);
  }
}

function renderScanBanner(scan, intervalSeconds) {
  if (!scan) return;
  document.getElementById("scan-status-text").textContent = scan.status || "Idle";

  const lastEl = document.getElementById("scan-last");
  if (scan.last_scan_at) {
    const secondsAgo = Math.floor(Date.now() / 1000 - scan.last_scan_at);
    const minsAgo = Math.floor(secondsAgo / 60);
    const nextInMins = Math.max(0, Math.floor((intervalSeconds - secondsAgo) / 60));
    lastEl.textContent = `Last scan: ${minsAgo < 1 ? "just now" : minsAgo + "m ago"} · Next in ~${nextInMins}m`;
  } else {
    lastEl.textContent = "Last scan: not yet";
  }
  document.getElementById("scan-count").textContent = `${scan.scan_count || 0} scans total`;
}

function renderCard(draft) {
  const badgeType = draft.draft_type;
  const badgeLabel = BADGE_LABELS[badgeType] || badgeType;
  const initial = (draft.author_name || "?").charAt(0).toUpperCase();
  const isKnowledgeQuestion = badgeType === "knowledge_question";

  return `
    <div class="card" data-id="${draft.id}" data-type="${badgeType}">
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
        ${isKnowledgeQuestion
          ? `<p class="post-text" style="font-weight:600;">${escapeHtml(draft.draft_text)}</p>
             <textarea class="draft-text" rows="2" placeholder="Type your answer..."></textarea>`
          : `<textarea class="draft-text" rows="3">${escapeHtml(draft.draft_text)}</textarea>`
        }
        <div class="card-actions">
          <button class="btn-approve" data-action="approve">${isKnowledgeQuestion ? "Save Answer" : "Approve &amp; Open X"}</button>
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
        if (result.intent_url) window.open(result.intent_url, "_blank");
        replaceWithDoneCard(card, "approved");
      } else {
        alert(result.error || "Could not process this draft.");
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
  const isKnowledgeQuestion = card.dataset.type === "knowledge_question";
  let label;
  if (status === "skipped") {
    label = `Skipped — ${handle}`;
  } else if (isKnowledgeQuestion) {
    label = "Answer saved — it'll remember this";
  } else {
    label = `Opened X for ${handle} — tap Post there to send`;
  }
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

  const statusText = document.getElementById("chat-status-text");
  statusText.textContent = "PROCESSING...";

  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  const data = await res.json();
  container.innerHTML += `<div class="chat-msg assistant">${escapeHtml(data.reply)}</div>`;
  container.scrollTop = container.scrollHeight;

  statusText.textContent = "ONLINE — awaiting instruction";
}

document.getElementById("mode-toggle").addEventListener("click", () => {
  reviewMode = !reviewMode;
  reviewIndex = 0;
  const btn = document.getElementById("mode-toggle");
  btn.textContent = reviewMode ? "List view" : "Review mode";
  btn.classList.toggle("active", reviewMode);
  loadFeed();
});

document.getElementById("approve-all-btn").addEventListener("click", async () => {
  const btn = document.getElementById("approve-all-btn");
  const original = btn.textContent;
  btn.textContent = "Opening...";
  btn.disabled = true;

  try {
    const res = await fetch("/api/drafts/approve-all", { method: "POST" });
    const data = await res.json();
    let opened = 0;
    for (const r of data.results) {
      if (r.success && r.intent_url) {
        window.open(r.intent_url, "_blank");
        opened++;
      }
    }
    btn.textContent = `Opened ${opened}`;
  } catch (e) {
    btn.textContent = "Failed";
  }

  setTimeout(() => {
    btn.textContent = original;
    btn.disabled = false;
    loadFeed();
  }, 2000);
});

loadFeed();
setInterval(loadFeed, 8000);
