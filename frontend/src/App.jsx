import React, { useEffect, useRef, useState } from "react";
import {
  CognitoIdentityProviderClient,
  SignUpCommand,
  InitiateAuthCommand,
} from "@aws-sdk/client-cognito-identity-provider";

const SUGGESTIONS = [
  "How much PTO do I have left?",
  "What's the remote work equipment policy?",
  "Submit a PTO request for next Monday, reason: vacation",
  "How does 401(k) matching work?",
];

function useConfig() {
  const [config, setConfig] = useState(null);
  useEffect(() => {
    fetch("/config.json")
      .then((r) => r.json())
      .then(setConfig)
      .catch(() => setConfig({}));
  }, []);
  return config;
}

function AuthPanel({ config, onAuthenticated }) {
  const [mode, setMode] = useState("signin");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);

  const client = new CognitoIdentityProviderClient({ region: config.region });

  const handleSignUp = async (e) => {
    e.preventDefault();
    setError("");
    setInfo("");
    setBusy(true);
    try {
      await client.send(
        new SignUpCommand({
          ClientId: config.userPoolClientId,
          Username: username,
          Password: password,
        })
      );
      setInfo("Account created and auto-confirmed. Signing you in...");
      await handleSignIn(e, true);
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleSignIn = async (e, skipPreventDefault) => {
    if (!skipPreventDefault) e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const resp = await client.send(
        new InitiateAuthCommand({
          AuthFlow: "USER_PASSWORD_AUTH",
          ClientId: config.userPoolClientId,
          AuthParameters: { USERNAME: username, PASSWORD: password },
        })
      );
      const accessToken = resp.AuthenticationResult.AccessToken;
      onAuthenticated({ username, accessToken });
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-box">
      <h2>{mode === "signin" ? "Sign in" : "Create an account"}</h2>
      <form onSubmit={mode === "signin" ? handleSignIn : handleSignUp}>
        <div className="field">
          <label>Username</label>
          <input value={username} onChange={(e) => setUsername(e.target.value)} required />
        </div>
        <div className="field">
          <label>Password (6+ characters)</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={6}
            required
          />
        </div>
        <button className="btn" type="submit" disabled={busy || !config.userPoolClientId}>
          {mode === "signin" ? "Sign in" : "Create account"}
        </button>
      </form>
      {error && <div className="error-msg">{error}</div>}
      {info && <div className="info-msg">{info}</div>}
      <p style={{ marginTop: 16, fontSize: 13 }}>
        {mode === "signin" ? (
          <>
            New here?{" "}
            <button className="btn secondary" onClick={() => setMode("signup")}>
              Create an account
            </button>
          </>
        ) : (
          <>
            Already have an account?{" "}
            <button className="btn secondary" onClick={() => setMode("signin")}>
              Sign in
            </button>
          </>
        )}
      </p>
    </div>
  );
}

function ChatPanel({ config, session, onSignOut }) {
  const [messages, setMessages] = useState([
    {
      role: "system",
      text: "This assistant answers questions grounded in real organizational policy documents, can check your leave balance, and can submit a formal request on your behalf. All messages pass through a Bedrock Guardrail that masks PII.",
    },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const logRef = useRef(null);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [messages]);

  const send = async (text) => {
    if (!text.trim() || busy) return;
    const history = messages
      .filter((m) => m.role === "user" || m.role === "assistant")
      .map((m) => ({ role: m.role, text: m.text }));

    setMessages((m) => [...m, { role: "user", text }]);
    setInput("");
    setBusy(true);
    try {
      const resp = await fetch(config.apiUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${session.accessToken}`,
        },
        body: JSON.stringify({ message: text, history }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        setMessages((m) => [...m, { role: "system", text: `Error: ${data.error || resp.statusText}` }]);
      } else {
        const extra = data.guardrail_intervened ? "  (Guardrail applied to this exchange)" : "";
        setMessages((m) => [...m, { role: "assistant", text: (data.response || "") + extra }]);
      }
    } catch (err) {
      setMessages((m) => [...m, { role: "system", text: `Network error: ${err.message}` }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="chat-log" ref={logRef}>
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            {m.text}
          </div>
        ))}
        {busy && <div className="msg assistant">Thinking...</div>}
      </div>
      <div className="suggestions">
        {SUGGESTIONS.map((s) => (
          <button key={s} onClick={() => send(s)} disabled={busy}>
            {s}
          </button>
        ))}
      </div>
      <div className="composer">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send(input)}
          placeholder="Ask about policy, leave balance, or submit a request..."
        />
        <button className="btn" onClick={() => send(input)} disabled={busy}>
          Send
        </button>
      </div>
    </>
  );
}

export default function App() {
  const config = useConfig();
  const [session, setSession] = useState(null);

  if (!config) return null;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <h1>Policy &amp; Benefits Assistant</h1>
          <div className="sub">Powered by Amazon Bedrock AgentCore</div>
        </div>
        {session && (
          <button className="signout" onClick={() => setSession(null)}>
            Sign out ({session.username})
          </button>
        )}
      </header>
      {session ? (
        <ChatPanel config={config} session={session} onSignOut={() => setSession(null)} />
      ) : (
        <AuthPanel config={config} onAuthenticated={setSession} />
      )}
    </div>
  );
}
