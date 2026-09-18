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

const WEIGHT_KEYS = ["quality", "cost", "latency"];
const WEIGHT_LABELS = { quality: "Quality", cost: "Cost", latency: "Latency" };

function rerank(candidates, weights) {
  return candidates
    .map((c) => ({
      ...c,
      final_score:
        (weights.quality / 100) * c.quality_score +
        (weights.cost / 100) * c.cost_score +
        (weights.latency / 100) * (c.latency_score || 0),
    }))
    .sort((a, b) => b.final_score - a.final_score);
}

function ModelTuningPanel({ config }) {
  const [weights, setWeights] = useState(null);
  const [candidates, setCandidates] = useState([]);
  const [ranking, setRanking] = useState([]);
  const [currentModel, setCurrentModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const load = async () => {
    try {
      const resp = await fetch(config.modelSelectorUrl);
      const data = await resp.json();
      setWeights(data.weights);
      setCandidates(data.model_candidates);
      setRanking(data.ranking.length ? data.ranking : rerank(data.model_candidates, data.weights));
      setCurrentModel(data.current_model);
    } catch (err) {
      setStatus(`Couldn't load model tuning state: ${err.message}`);
    } finally {
      setLoaded(true);
    }
  };

  useEffect(() => {
    if (open && !loaded) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Moving one slider proportionally redistributes the delta across the
  // other two, clamped to [0, 100], so the three always sum to exactly 100.
  const onSlide = (key, value) => {
    const next = { ...weights, [key]: value };
    const others = WEIGHT_KEYS.filter((k) => k !== key);
    const remaining = 100 - value;
    const othersSum = others.reduce((s, k) => s + weights[k], 0);
    if (othersSum === 0) {
      next[others[0]] = remaining;
      next[others[1]] = 0;
    } else {
      next[others[0]] = Math.round((weights[others[0]] / othersSum) * remaining);
      next[others[1]] = remaining - next[others[0]];
    }
    setWeights(next);
    setRanking(rerank(candidates, next));
  };

  const evaluateAndApply = async () => {
    setBusy(true);
    setStatus("Evaluating live latency across all 5 models and checking whether a switch is warranted -- this can take up to a couple of minutes if the live model actually changes...");
    try {
      const resp = await fetch(config.modelSelectorUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ weights }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        setStatus(`Error: ${data.error || resp.statusText}`);
      } else {
        setWeights(data.weights);
        setRanking(data.ranking);
        setCurrentModel(data.current_model);
        setStatus(
          data.switched
            ? `Switched the live app from ${data.previous_model} to ${data.current_model}.`
            : `No switch needed -- ${data.current_model} already best fits these preferences.`
        );
      }
    } catch (err) {
      setStatus(`Network error: ${err.message}`);
    } finally {
      setBusy(false);
    }
  };

  if (!config.modelSelectorUrl) return null;

  return (
    <div className="tuning-box">
      <button className="tuning-toggle" onClick={() => setOpen((o) => !o)}>
        {open ? "Hide" : "Show"} Model Tuning (Bedrock flexibility demo) {open ? "▲" : "▼"}
      </button>
      {open && (
        <div className="tuning-body">
          {!weights ? (
            <div className="info-msg">Loading...</div>
          ) : (
            <>
              <p className="tuning-blurb">
                Adjust how much each preference matters, then evaluate which of 5 candidate
                LLMs best fits that tradeoff. Quality and cost are illustrative reference
                scores; latency is measured live, fresh, every time you click Evaluate.
                If a different model wins, the live app is actually redirected to it.
              </p>
              {WEIGHT_KEYS.map((key) => (
                <div className="slider-row" key={key}>
                  <label>{WEIGHT_LABELS[key]}: {weights[key]}%</label>
                  <input
                    type="range"
                    min="0"
                    max="100"
                    value={weights[key]}
                    onChange={(e) => onSlide(key, Number(e.target.value))}
                  />
                </div>
              ))}
              <div className="weight-sum">Total: {WEIGHT_KEYS.reduce((s, k) => s + weights[k], 0)}%</div>
              <button className="btn" onClick={evaluateAndApply} disabled={busy}>
                {busy ? "Evaluating..." : "Evaluate & Apply"}
              </button>
              {status && <div className="info-msg">{status}</div>}
              <table className="ranking-table">
                <thead>
                  <tr><th>Model</th><th>Quality</th><th>Cost</th><th>Latency</th><th>Score</th><th></th></tr>
                </thead>
                <tbody>
                  {ranking.map((r) => (
                    <tr key={r.id} className={r.id === currentModel ? "live-row" : ""}>
                      <td>{r.label}</td>
                      <td>{r.quality_score}</td>
                      <td>{r.cost_score}</td>
                      <td>{Math.round(r.latency_score || 0)}</td>
                      <td>{Number(r.final_score).toFixed(1)}</td>
                      <td>{r.id === currentModel ? "● live" : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}
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
        <div className="header-actions">
          {config.insightsUrl && (
            <a className="insights-link" href={config.insightsUrl} target="_blank" rel="noreferrer">
              Class Insights &rarr;
            </a>
          )}
          {session && (
            <button className="signout" onClick={() => setSession(null)}>
              Sign out ({session.username})
            </button>
          )}
        </div>
      </header>
      {session ? (
        <ChatPanel config={config} session={session} onSignOut={() => setSession(null)} />
      ) : (
        <AuthPanel config={config} onAuthenticated={setSession} />
      )}
      {/* Deliberately outside the signed-in gate: Tommy asked that this
          feature (both the UI and its backend) require no authentication,
          same as the Class Insights companion app. */}
      <ModelTuningPanel config={config} />
    </div>
  );
}
