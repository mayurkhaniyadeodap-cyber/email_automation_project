import { useEffect, useState } from "react";
import { saveSettings } from "./saveSettings";

// Per-brand live-data credentials (doc §8). Stored on BrandSettings.integrations.
export default function Integrations({ settings, brandId, reload }) {
  const [cfg, setCfg] = useState({ shopify: {}, shipping: {}, gokwik: {}, gallabox: {} });
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const c = settings?.integrations || {};
    setCfg({
      shopify: { shop: "", token: "", api_version: "", ...(c.shopify || {}) },
      shipping: { base_url: "", api_key: "", ...(c.shipping || {}) },
      gokwik: { base_url: "", api_key: "", ...(c.gokwik || {}) },
      gallabox: { api_key: "", api_secret: "", base_url: "", ...(c.gallabox || {}) },
    });
  }, [settings]);

  const set = (group, key, value) =>
    setCfg((c) => ({ ...c, [group]: { ...c[group], [key]: value } }));

  function prune(obj) {
    // Drop empty fields so we don't store blank creds.
    const out = {};
    Object.entries(obj).forEach(([k, v]) => {
      if (v !== "" && v != null) out[k] = v;
    });
    return out;
  }

  async function save() {
    setBusy(true);
    setMsg("");
    setError("");
    const integrations = {};
    const sh = prune(cfg.shopify);
    const sp = prune(cfg.shipping);
    const gk = prune(cfg.gokwik);
    const gb = prune(cfg.gallabox);
    if (Object.keys(sh).length) integrations.shopify = sh;
    if (Object.keys(sp).length) integrations.shipping = sp;
    if (Object.keys(gk).length) integrations.gokwik = gk;
    if (Object.keys(gb).length) integrations.gallabox = gb;
    try {
      await saveSettings(settings, brandId, { integrations });
      setMsg("Saved.");
      reload();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const field = (group, name, label, type = "text") => (
    <div className="field">
      <label>{label}</label>
      <input
        type={type}
        value={cfg[group][name] || ""}
        onChange={(e) => set(group, name, e.target.value)}
      />
    </div>
  );

  return (
    <div className="card" style={{ maxWidth: 640 }}>
      <h3>Shopify (order / EDD)</h3>
      {field("shopify", "shop", "Shop domain (x.myshopify.com)")}
      {field("shopify", "token", "Admin API token", "password")}
      {field("shopify", "api_version", "API version (optional)")}

      <h3 style={{ marginTop: 20 }}>Shipping Portal (tracking)</h3>
      {field("shipping", "base_url", "Base URL")}
      {field("shipping", "api_key", "API key", "password")}

      <h3 style={{ marginTop: 20 }}>GoKwik (payment)</h3>
      {field("gokwik", "base_url", "Base URL")}
      {field("gokwik", "api_key", "API key", "password")}

      <h3 style={{ marginTop: 20 }}>Gallabox (ticket sync)</h3>
      {field("gallabox", "api_key", "API key", "password")}
      {field("gallabox", "api_secret", "API secret", "password")}
      {field("gallabox", "base_url", "Base URL (blank = server.gallabox.com)")}

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
