const thread   = document.getElementById("thread");
const input    = document.getElementById("msg-input");
const sendBtn  = document.getElementById("send-btn");
const welcome  = document.getElementById("welcome");

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

let history = [];   // [{role, content}] sent to /api/chat

function newConversation() {
  history = [];
  thread.innerHTML = "";
  thread.appendChild(welcome);
  welcome.style.display = "";
  input.value = "";
  autoResize(input);
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
      tag.textContent = s;
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
      body: JSON.stringify({ question: text, history }),
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
      appendMessage("assistant", answer, sources);
      history.push({ role: "assistant", content: answer });
    }
  } catch (e) {
    typingRow.remove();
    appendError("Could not reach the chat backend. Is the server running?");
    history.pop();
  }

  busy = false;
  sendBtn.disabled = false;
  input.disabled = false;
  input.focus();
}
