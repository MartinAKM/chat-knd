const thread  = document.getElementById("thread");
const input   = document.getElementById("msg-input");
const sendBtn = document.getElementById("send-btn");
const welcome = document.getElementById("welcome");

marked.use({ breaks: true, gfm: true });

let busy = false;
let attachedImages = []; // { dataUrl, base64 }

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
  attachedImages = [];
  renderImagePreviews();
  document.querySelectorAll(".history-item").forEach(el => el.classList.remove("active"));
  input.focus();
}

// ── Image attachment ───────────────────────────────────────────────────────

input.addEventListener("paste", e => {
  const items = Array.from(e.clipboardData?.items || []);
  const imageItems = items.filter(it => it.type.startsWith("image/"));
  if (!imageItems.length) return;
  e.preventDefault();
  imageItems.forEach(item => {
    const file = item.getAsFile();
    if (!file) return;
    const reader = new FileReader();
    reader.onload = ev => {
      const dataUrl = ev.target.result;
      attachedImages.push({ dataUrl, base64: dataUrl.split(",")[1] });
      renderImagePreviews();
    };
    reader.readAsDataURL(file);
  });
});

function handleImageAttach() {
  const imgInput = document.getElementById("img-input");
  for (const file of imgInput.files) {
    if (!file.type.startsWith("image/")) continue;
    const reader = new FileReader();
    reader.onload = e => {
      const dataUrl = e.target.result;
      const base64  = dataUrl.split(",")[1];
      attachedImages.push({ dataUrl, base64 });
      renderImagePreviews();
    };
    reader.readAsDataURL(file);
  }
  imgInput.value = "";
}

function renderImagePreviews() {
  const preview = document.getElementById("img-preview");
  preview.innerHTML = "";
  if (!attachedImages.length) {
    preview.style.display = "none";
    return;
  }
  preview.style.display = "flex";
  attachedImages.forEach((img, i) => {
    const wrap = document.createElement("div");
    wrap.className = "preview-thumb";

    const im = document.createElement("img");
    im.src = img.dataUrl;

    const btn = document.createElement("button");
    btn.className = "preview-remove";
    btn.textContent = "×";
    btn.onclick = () => { attachedImages.splice(i, 1); renderImagePreviews(); };

    wrap.appendChild(im);
    wrap.appendChild(btn);
    preview.appendChild(wrap);
  });
}

// ── Rendering helpers ──────────────────────────────────────────────────────

function buildSourcesBlock(sources) {
  if (!sources || !sources.length) return null;
  const details = document.createElement("details");
  details.className = "msg-sources";

  const summary = document.createElement("summary");
  summary.className = "sources-toggle";
  summary.textContent = sources.length === 1 ? "1 fonte" : `${sources.length} fontes`;
  details.appendChild(summary);

  const list = document.createElement("div");
  list.className = "sources-tags";
  sources.forEach(s => {
    const tag = document.createElement("span");
    tag.className = "source-tag";
    if (s.includes("ticket_")) {
      const link = document.createElement("a");
      link.href = `https://kundencloud.com.br:3825/atendimento?id=${s.replace("ticket_", "")}&callType=customer`;
      link.target = "_blank";
      link.className = "source-tag-link";
      link.textContent = s;
      tag.appendChild(link);
    } else {
      tag.textContent = s;
    }
    list.appendChild(tag);
  });
  details.appendChild(list);
  return details;
}

function appendMessage(role, text, sources, images) {
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
    if (images && images.length) {
      const imgWrap = document.createElement("div");
      imgWrap.className = "msg-images";
      images.forEach(dataUrl => {
        const img = document.createElement("img");
        img.src = dataUrl;
        img.className = "msg-image";
        imgWrap.appendChild(img);
      });
      bubble.appendChild(imgWrap);
    }
    const textSpan = document.createElement("span");
    textSpan.textContent = text;
    bubble.appendChild(textSpan);
  }

  const sourcesBlock = buildSourcesBlock(sources);
  if (sourcesBlock) bubble.appendChild(sourcesBlock);

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
  const text   = input.value.trim();
  const images = [...attachedImages];
  if (!text && !images.length) return;

  busy = true;
  sendBtn.disabled = true;
  input.disabled = true;

  input.value = "";
  autoResize(input);
  attachedImages = [];
  renderImagePreviews();

  const dataUrls = images.map(i => i.dataUrl);
  const base64s  = images.map(i => i.base64);

  appendMessage("user", text, [], dataUrls);
  history.push({ role: "user", content: text });

  const typingRow = appendTyping();

  let assistantRow    = null;
  let assistantBubble = null;
  let fullAnswer      = "";
  let rafId           = null;

  function scheduleRender() {
    if (rafId) return;
    rafId = requestAnimationFrame(() => {
      rafId = null;
      if (!assistantBubble) return;
      assistantBubble.innerHTML = marked.parse(fullAnswer);
      thread.scrollTop = thread.scrollHeight;
    });
  }

  function createStreamingBubble() {
    typingRow.remove();
    assistantRow = document.createElement("div");
    assistantRow.className = "msg-row assistant";
    assistantBubble = document.createElement("div");
    assistantBubble.className = "msg-bubble streaming";
    const meta = document.createElement("div");
    meta.className = "msg-meta";
    meta.textContent = "ChatKND";
    assistantRow.appendChild(assistantBubble);
    assistantRow.appendChild(meta);
    thread.appendChild(assistantRow);
  }

  function finalizeStreamingBubble(sources) {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    if (!assistantBubble) return;
    assistantBubble.innerHTML = marked.parse(fullAnswer);
    assistantBubble.classList.remove("streaming");
    renderMathInElement(assistantBubble, {
      delimiters: [
        { left: "$$", right: "$$", display: true  },
        { left: "$",  right: "$",  display: false },
        { left: "\\[", right: "\\]", display: true  },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
    });
    const sourcesBlock = buildSourcesBlock(sources);
    if (sourcesBlock) assistantBubble.appendChild(sourcesBlock);
  }

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: text,
        history,
        conversation_id: currentConversationId,
        images: base64s.length ? base64s : undefined,
      }),
    });

    if (!res.ok) {
      typingRow.remove();
      const err = await res.json().catch(() => ({}));
      appendError(err.error || `Erro interno (${res.status})`);
      history.pop();
    } else {
      const reader  = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let hadError = false;

      outer: while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          let chunk;
          try { chunk = JSON.parse(line.slice(6)); } catch { continue; }

          if (chunk.error) {
            typingRow.remove();
            appendError(chunk.error);
            history.pop();
            hadError = true;
            break outer;
          }

          if (chunk.token) {
            if (!assistantRow) createStreamingBubble();
            fullAnswer += chunk.token;
            scheduleRender();
          }

          if (chunk.done) {
            finalizeStreamingBubble(chunk.sources);
            if (chunk.conversation_id) currentConversationId = chunk.conversation_id;
            history.push({ role: "assistant", content: fullAnswer });
            loadHistory();
          }
        }
      }

      if (!hadError && !assistantRow) {
        typingRow.remove();
        appendError("Sem resposta.");
        history.pop();
      }
    }
  } catch (e) {
    typingRow.remove();
    appendError("Não foi possível estabelecer conexão com o sistema de respostas. Erro: " + e);
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
  if (diffDays === 0) return "Hoje";
  if (diffDays === 1) return "Ontem";
  if (diffDays < 7)  return d.toLocaleDateString("pt-BR", { weekday: "long" });
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
    empty.textContent = "Você não conversou comigo ainda :(";
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
      del.title = "Deletar conversa";
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
    console.error("Falha ao carregar a conversa:", e);
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
    console.error("Falha ao deletar a conversa:", e);
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
