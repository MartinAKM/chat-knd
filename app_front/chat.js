const thread  = document.getElementById("thread");
const input   = document.getElementById("msg-input");
const sendBtn = document.getElementById("send-btn");
const welcome = document.getElementById("welcome");

marked.use({ breaks: true, gfm: true });

let busy = false;

// ── Input helpers ──────────────────────────────────────────────────────────

function autoResize(el) {
  el.style.height = "auto";
  el.style.height = el.scrollHeight + "px";
}

function onKey(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

// ── Conversation state ─────────────────────────────────────────────────────

let history = [];
let currentConversationId = null;

function newConversation() {
  currentConversationId = null;
  history = [];
  thread.innerHTML = "";
  thread.appendChild(welcome);
  welcome.style.display = "";
  input.value = "";
  autoResize(input);
  document.querySelectorAll(".history-item").forEach(el => el.classList.remove("active"));
  input.focus();
}

// ── Rendering helpers ──────────────────────────────────────────────────────

function appendMessage(role, text, sources) {
  welcome.style.display = "none";

  const row = document.createElement("div");
  row.className = `msg-row ${role}`;

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";

  if (role === "assistant") {
    bubble.innerHTML = marked.parse(text);
    renderMathInElement(bubble, {
      delimiters: [
        { left: "$$", right: "$$", display: true  },
        { left: "$",  right: "$",  display: false },
        { left: "\\[", right: "\\]", display: true  },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
    });
  } else {
    bubble.textContent = text;
  }

  if (sources && sources.length) {
    const bar = document.createElement("div");
    bar.className = "msg-sources";
    sources.forEach(s => {
      const tag = document.createElement("span");
      tag.className = "source-tag";

      if (s.includes("ticket_")) {
        const link = document.createElement("a");
        const ticketNumber = s.replace("ticket_", "");
        link.href = `https://kundencloud.com.br:3825/atendimento?id=${ticketNumber}&callType=customer`;
        link.target = "_blank";
        link.className = "source-tag-link";
        link.textContent = s;
        tag.appendChild(link);
      } else {
        tag.textContent = s;
      }

      bar.appendChild(tag);
    });
    bubble.appendChild(bar);
  }

  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.textContent = role === "user" ? "You" : "ChatKND";

  row.appendChild(bubble);
  row.appendChild(meta);
  thread.appendChild(row);
  thread.scrollTop = thread.scrollHeight;
  return row;
}

function appendTyping() {
  const row = document.createElement("div");
  row.className = "msg-row assistant";

  const bubble = document.createElement("div");
  bubble.className = "typing-bubble";
  bubble.innerHTML = "<span></span><span></span><span></span>";

  row.appendChild(bubble);
  thread.appendChild(row);
  thread.scrollTop = thread.scrollHeight;
  return row;
}

function appendError(text) {
  const row = document.createElement("div");
  row.className = "msg-row assistant";

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble error";
  bubble.textContent = text;

  row.appendChild(bubble);
  thread.appendChild(row);
  thread.scrollTop = thread.scrollHeight;
}

// ── Main send logic ────────────────────────────────────────────────────────

async function sendMessage() {
  if (busy) return;
  const text = input.value.trim();
  if (!text) return;

  busy = true;
  sendBtn.disabled = true;
  input.disabled = true;

  input.value = "";
  autoResize(input);

  appendMessage("user", text);
  history.push({ role: "user", content: text });

  const typingRow = appendTyping();

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: text, history, conversation_id: currentConversationId }),
    });

    typingRow.remove();

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      appendError(err.error || `Server error (${res.status})`);
      history.pop();
    } else {
      const data = await res.json();
      const answer = data.answer ?? "(empty response)";
      const sources = data.sources ?? [];

      if (data.conversation_id) {
        currentConversationId = data.conversation_id;
      }

      appendMessage("assistant", answer, sources);
      history.push({ role: "assistant", content: answer });
      loadHistory();
    }
  } catch (e) {
    typingRow.remove();
    appendError("Could not reach the chat backend. Err: " + e);
    history.pop();
  }

  busy = false;
  sendBtn.disabled = false;
  input.disabled = false;
  input.focus();
}

// ── History sidebar ────────────────────────────────────────────────────────

function formatDate(isoStr) {
  const d    = new Date(isoStr);
  const now  = new Date();
  const diffDays = Math.floor((now - d) / 86400000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7)  return d.toLocaleDateString("pt-BR", { weekday: "short" });
  return d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" });
}

async function loadHistory() {
  try {
    const res  = await fetch("/api/history");
    const data = await res.json();
    renderHistoryList(data.conversations || []);
  } catch (_) { /* sidebar is non-critical */ }
}

function renderHistoryList(conversations) {
  const list = document.getElementById("history-list");
  list.innerHTML = "";

  if (!conversations.length) {
    const empty = document.createElement("p");
    empty.style.cssText = "color:rgba(255,255,255,.3);font-size:.75rem;padding:12px 8px;";
    empty.textContent = "No conversations yet.";
    list.appendChild(empty);
    return;
  }

  // Group by date label
  const groups = new Map();
  conversations.forEach(conv => {
    const label = formatDate(conv.updated_at);
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(conv);
  });

  groups.forEach((convs, label) => {
    const section = document.createElement("div");
    section.className = "history-section-label";
    section.textContent = label;
    list.appendChild(section);

    convs.forEach(conv => {
      const item = document.createElement("div");
      item.className = "history-item" + (conv.id === currentConversationId ? " active" : "");
      item.dataset.id = conv.id;

      const title = document.createElement("span");
      title.className = "history-title";
      title.textContent = conv.title;

      const del = document.createElement("button");
      del.className = "history-delete";
      del.textContent = "×";
      del.title = "Delete conversation";
      del.onclick = async e => {
        e.stopPropagation();
        await deleteConversation(conv.id);
      };

      item.appendChild(title);
      item.appendChild(del);
      item.onclick = () => loadConversation(conv.id);
      list.appendChild(item);
    });
  });
}

async function loadConversation(id) {
  try {
    const res = await fetch(`/api/history/${id}`);
    if (!res.ok) return;
    const conv = await res.json();

    history = [];
    thread.innerHTML = "";
    thread.appendChild(welcome);
    welcome.style.display = "none";
    currentConversationId = id;

    for (const msg of conv.messages) {
      if (msg.role === "user") {
        appendMessage("user", msg.content);
        history.push({ role: "user", content: msg.content });
      } else if (msg.role === "assistant") {
        appendMessage("assistant", msg.content, msg.sources || []);
        history.push({ role: "assistant", content: msg.content });
      }
    }

    document.querySelectorAll(".history-item").forEach(el => {
      el.classList.toggle("active", el.dataset.id === id);
    });

    input.focus();
  } catch (e) {
    console.error("Failed to load conversation:", e);
  }
}

async function deleteConversation(id) {
  const result = await Swal.fire({
    text: "Tem certeza que deseja deletar essa conversa?",
    icon: "question",
    showCancelButton: true,
    confirmButtonColor: "#1e3a5f",
    cancelButtonColor: "#6b7280",
    confirmButtonText: "Deletar",
    cancelButtonText: "Cancelar",
    customClass: { popup: "custom-swal" },
    heightAuto: false,
  });
  if (!result.isConfirmed) return;
  try {
    await fetch(`/api/history/${id}`, { method: "DELETE" });
    if (id === currentConversationId) newConversation();
    await loadHistory();
  } catch (e) {
    console.error("Failed to delete conversation:", e);
  }
}

// ── Auth ───────────────────────────────────────────────────────────────────

async function initUser() {
  try {
    const res = await fetch("/api/auth/me");
    if (res.status === 401) { window.location.href = "/login"; return; }
    const user = await res.json();
    document.getElementById("header-user-name").textContent = user.name;
    if (user.role === "admin") {
      document.getElementById("nav-viewer").style.display = "";
    }
  } catch (_) {}
}

async function logout() {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.href = "/login";
}

// ── Init ───────────────────────────────────────────────────────────────────

initUser();
loadHistory();
