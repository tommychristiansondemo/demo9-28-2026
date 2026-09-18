const logEl = document.getElementById("chat-log");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send");
const suggestionsEl = document.getElementById("suggestions");

let config = { apiUrl: "" };
let messages = [];
let busy = false;

function addMessage(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = text;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
  return div;
}

async function send(text) {
  if (!text.trim() || busy) return;
  busy = true;
  sendBtn.disabled = true;
  addMessage("user", text);
  const history = messages.slice();
  messages.push({ role: "user", text });
  inputEl.value = "";
  const thinking = addMessage("assistant", "Thinking...");
  try {
    const resp = await fetch(config.apiUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, history }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      thinking.textContent = `Error: ${data.error || resp.statusText}`;
      thinking.className = "msg system";
    } else {
      thinking.textContent = data.response || "";
      messages.push({ role: "assistant", text: data.response || "" });
    }
  } catch (err) {
    thinking.textContent = `Network error: ${err.message}`;
    thinking.className = "msg system";
  } finally {
    busy = false;
    sendBtn.disabled = false;
  }
}

sendBtn.addEventListener("click", () => send(inputEl.value));
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") send(inputEl.value);
});
suggestionsEl.addEventListener("click", (e) => {
  const q = e.target.getAttribute("data-q");
  if (q) send(q);
});

fetch("/config.json")
  .then((r) => r.json())
  .then((c) => {
    config = c;
    addMessage("system", "Ask anything about how the Policy & Benefits Assistant or this Insights app were built and deployed.");
  })
  .catch(() => addMessage("system", "Failed to load configuration."));
