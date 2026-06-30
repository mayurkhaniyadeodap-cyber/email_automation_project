import { useEffect, useState } from "react";
import { saveSettings } from "./saveSettings";

const ACTIONS = [
  ["info_only", "Info only"],
  ["await_evidence", "Await evidence"],
  ["create_ticket", "Create ticket"],
  ["update_system", "Update in system"],
  ["continue_check", "Continue to next check"],
  ["trigger_cancellation_refund_pickup", "Trigger cancel / refund / pickup"],
];
const MODES = [
  ["", "(default)"],
  ["auto_send", "Auto-send"],
  ["draft", "Draft"],
  ["off", "Off"],
];

export default function General({ settings, brandId, reload }) {
  const [form, setForm] = useState(null);
  const [apiKey, setApiKey] = useState("");
  const [slaText, setSlaText] = useState("{}");
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setForm({
      ai_provider: settings?.ai_provider || "gemini",
      ai_model: settings?.ai_model || "",
      confidence_threshold: settings?.confidence_threshold ?? 0.75,
      await_evidence_autosend: settings?.await_evidence_autosend ?? true,
      holding_reply: settings?.holding_reply || "",
      automation_toggles: { ...(settings?.automation_toggles || {}) },
    });
    setSlaText(JSON.stringify(settings?.sla_config || {}, null, 2));
    setApiKey("");
  }, [settings]);

  if (!form) return null;

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));
  const setToggle = (action, v) =>
    setForm((f) => {
      const t = { ...f.automation_toggles };
      if (v) t[action] = v;
      else delete t[action];
      return { ...f, automation_toggles: t };
    });

  async function save() {
    setBusy(true);
    setMsg("");
    setError("");
    let sla;
    try {
      sla = JSON.parse(slaText || "{}");
    } catch {
      setError("SLA config is not valid JSON.");
      setBusy(false);
      return;
    }
    const patch = {
      ai_provider: form.ai_provider,
      ai_model: form.ai_model,
      confidence_threshold: Number(form.confidence_threshold),
      await_evidence_autosend: form.await_evidence_autosend,
      holding_reply: form.holding_reply,
      automation_toggles: form.automation_toggles,
      sla_config: sla,
    };
    if (apiKey.trim()) patch.ai_api_key = apiKey.trim();
    try {
      await saveSettings(settings, brandId, patch);
      setMsg("Saved.");
      reload();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ maxWidth: 640 }}>
      <h3>AI provider</h3>
      <div className="field">
        <label>Provider</label>
        <select value={form.ai_provider} onChange={(e) => set("ai_provider", e.target.value)}>
          <option value="gemini">Gemini</option>
          <option value="chatgpt">ChatGPT</option>
        </select>
      </div>
      <div className="field">
        <label>Model (blank = provider default)</label>
        <input
          value={form.ai_model}
          placeholder="gemini-1.5-flash / gpt-4o-mini"
          onChange={(e) => set("ai_model", e.target.value)}
        />
      </div>
      <div className="field">
        <label>
          API key {settings?.ai_key_set ? "(configured — leave blank to keep)" : "(not set)"}
        </label>
        <input
          type="password"
          value={apiKey}
          placeholder={settings?.ai_key_set ? "••••••••" : "Paste API key"}
          onChange={(e) => setApiKey(e.target.value)}
        />
      </div>

      <h3 style={{ marginTop: 20 }}>Guardrails</h3>
      <div className="field">
        <label>Confidence threshold (0–1)</label>
        <input
          type="number"
          min="0"
          max="1"
          step="0.05"
          value={form.confidence_threshold}
          onChange={(e) => set("confidence_threshold", e.target.value)}
        />
      </div>
      <div className="field">
        <label>
          <input
            type="checkbox"
            checked={form.await_evidence_autosend}
            onChange={(e) => set("await_evidence_autosend", e.target.checked)}
          />{" "}
          Auto-send evidence requests (off = draft instead)
        </label>
      </div>
      <div className="field">
        <label>Holding reply (when AI can't handle it)</label>
        <textarea
          value={form.holding_reply}
          onChange={(e) => set("holding_reply", e.target.value)}
        />
      </div>

      <h3 style={{ marginTop: 20 }}>Automation toggles (per action)</h3>
      {ACTIONS.map(([action, label]) => (
        <div className="field" key={action}>
          <label>{label}</label>
          <select
            value={form.automation_toggles[action] || ""}
            onChange={(e) => setToggle(action, e.target.value)}
          >
            {MODES.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </div>
      ))}

      <h3 style={{ marginTop: 20 }}>SLA config (JSON)</h3>
      <div className="field">
        <textarea
          value={slaText}
          onChange={(e) => setSlaText(e.target.value)}
          style={{ fontFamily: "monospace", minHeight: 110 }}
        />
        <div className="muted" style={{ fontSize: 12 }}>
          e.g. <code>{`{"3": {"first_response_mins": 120}}`}</code>
        </div>
      </div>

      <div className="row" style={{ marginTop: 12 }}>
        <button className="btn primary" onClick={save} disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </button>
        {msg && <span style={{ color: "var(--green)" }}>{msg}</span>}
        {error && <span className="error">{error}</span>}
      </div>
    </div>
  );
}
