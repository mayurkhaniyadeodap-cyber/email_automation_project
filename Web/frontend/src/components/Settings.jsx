import { useEffect, useState } from "react";
import { api } from "../api";
import { useScope } from "../scope.jsx";
import Integrations from "./settings/Integrations.jsx";
import BlockList from "./settings/BlockList.jsx";
import Taxonomy from "./settings/Taxonomy.jsx";
import Mailboxes from "./settings/Mailboxes.jsx";
import SupportEmails from "./settings/SupportEmails.jsx";

const TABS = [
  ["gmail", "Gmail"],
  ["support_emails", "Support Emails"],
  ["blocklist", "Block list"],
  ["integrations", "Integrations"],
  ["taxonomy", "Categories & rules"],
];

export default function Settings() {
  const { orgId, brandId } = useScope();
  const [tab, setTab] = useState("gmail");
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState("");

  async function loadSettings() {
    if (!brandId) return;
    setError("");
    try {
      const data = await api.get("/settings/", { organization: orgId, brand: brandId });
      const rows = data.results || data;
      setSettings(rows[0] || null);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    loadSettings();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId, brandId]);

  if (!brandId) return <div className="muted">Select a brand.</div>;

  return (
    <div>
      <div className="tabs">
        {TABS.map(([v, l]) => (
          <button
            key={v}
            className={`tab ${tab === v ? "active" : ""}`}
            onClick={() => setTab(v)}
          >
            {l}
          </button>
        ))}
      </div>

      {error && <div className="error">{error}</div>}

      {tab === "gmail" && <Mailboxes />}
      {tab === "support_emails" && <SupportEmails />}
      {tab === "integrations" && (
        <Integrations settings={settings} brandId={brandId} reload={loadSettings} />
      )}
      {tab === "blocklist" && <BlockList />}
      {tab === "taxonomy" && <Taxonomy />}
    </div>
  );
}
