import { useEffect, useState } from "react";
import { api } from "../../api";
import { useAuth } from "../../auth.jsx";
import { useScope } from "../../scope.jsx";

// Connect / fetch a brand's mailbox. IMAP (host+password in .env) needs no OAuth;
// Gmail uses the browser Connect flow.
export default function Mailboxes() {
  const { user } = useAuth();
  const { orgId, brandId } = useScope();
  const [mailboxes, setMailboxes] = useState([]);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const provider = user?.email_provider || "imap";
  const imapReady = !!user?.imap_configured;

  async function load() {
    if (!brandId) return;
    setError("");
    try {
      const data = await api.get("/mailboxes/", { organization: orgId, brand: brandId });
      setMailboxes(data.results || data);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId, brandId]);

  function connect(mb) {
    window.open(`/api/gmail/connect/?mailbox=${mb.id}`, "_blank");
    setMsg("A Google authorization tab opened. Approve access, then click “Fetch emails”.");
  }

  async function fetchNow(mb) {
    setBusy(`fetch-${mb.id}`);
    setMsg("");
    setError("");
    try {
      const res = await api.post(`/gmail/fetch/?mailbox=${mb.id}`);
      const n = res.fetched ?? res.ingested ?? 0;
      setMsg(
        n === 0
          ? "No new emails."
          : `Fetched ${n} new email${n === 1 ? "" : "s"}. Check the Tickets tab.`
      );
    } catch (err) {
      setError(err.data?.detail || err.message);
    } finally {
      setBusy("");
    }
  }

  const canFetch = (mb) =>
    provider === "imap" ? imapReady : mb.connected;

  return (
    <div className="card">
      <h3>Mailboxes</h3>
      <p className="muted" style={{ marginTop: 0 }}>
        Active provider: <b>{provider.toUpperCase()}</b>
        {provider === "imap" &&
          (imapReady ? " — configured in .env ✓" : " — set IMAP_HOST / IMAP_USER / IMAP_PASSWORD in backend/.env")}
      </p>

      {msg && <div style={{ color: "var(--green)", marginBottom: 10 }}>{msg}</div>}
      {error && <div className="error">{error}</div>}

      <table>
        <thead>
          <tr>
            <th>Mailbox</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {mailboxes.map((mb) => (
            <tr key={mb.id}>
              <td>{mb.email_address}</td>
              <td>
                {provider === "imap" ? (
                  imapReady ? (
                    <span className="badge auto_resolved">IMAP ready</span>
                  ) : (
                    <span className="badge">IMAP not set</span>
                  )
                ) : mb.connected ? (
                  <span className="badge auto_resolved">Connected</span>
                ) : (
                  <span className="badge">Not connected</span>
                )}
              </td>
              <td>
                <div className="row">
                  {provider === "gmail" && (
                    <button className="btn" onClick={() => connect(mb)}>
                      {mb.connected ? "Reconnect" : "Connect Gmail"}
                    </button>
                  )}
                  <button
                    className="btn primary"
                    disabled={!canFetch(mb) || busy === `fetch-${mb.id}`}
                    onClick={() => fetchNow(mb)}
                  >
                    {busy === `fetch-${mb.id}` ? "Fetching…" : "Fetch emails"}
                  </button>
                </div>
              </td>
            </tr>
          ))}
          {mailboxes.length === 0 && (
            <tr>
              <td colSpan={3} className="muted" style={{ textAlign: "center", padding: 18 }}>
                No mailboxes for this brand.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {provider === "imap" && (
        <div className="muted" style={{ fontSize: 12, marginTop: 12 }}>
          IMAP setup in <code>backend/.env</code>: e.g. Zoho India →{" "}
          <code>IMAP_HOST=imap.zoho.in</code>, Gmail → <code>imap.gmail.com</code>{" "}
          (use an <b>app password</b>), then restart the backend.
        </div>
      )}
    </div>
  );
}
