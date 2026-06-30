import { useEffect, useState } from "react";
import { api } from "../../api";
import { useScope } from "../../scope.jsx";

// Support Emails: the ONE fetched primary Gmail inbox + any "send mail as" ALIASES used only for
// sending replies. Fully dynamic -- add/edit/delete/activate here, no code changes. Any inbound
// email FROM one of these is treated as our own and is never imported (no self-reply loop).
export default function SupportEmails() {
  const { orgId, brandId } = useScope();
  const [rows, setRows] = useState([]);
  const [draft, setDraft] = useState({ email: "", owner_name: "", is_primary: false });
  const [editing, setEditing] = useState(null);   // {id, email, owner_name}
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    if (!brandId) return;
    setError("");
    try {
      const data = await api.get("/support-emails/", { organization: orgId, brand: brandId });
      setRows(data.results || data);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId, brandId]);

  async function add(e) {
    e.preventDefault();
    setError(""); setOk("");
    const email = draft.email.trim();
    if (!email) { setError("Enter an email address first."); return; }
    if (!brandId) { setError("Select a brand at the top first."); return; }
    setBusy(true);
    try {
      await api.post("/support-emails/", {
        brand: Number(brandId), email, owner_name: draft.owner_name.trim(),
        is_primary: draft.is_primary,
      });
      setDraft({ email: "", owner_name: "", is_primary: false });
      setOk(`Added ${email}.`);
      await load();
    } catch (err) {
      const data = err.data || {};
      setError(data.email?.[0] || data.non_field_errors?.[0] || data.detail ||
               err.message || "Could not add the email.");
    } finally {
      setBusy(false);
    }
  }

  async function saveEdit() {
    setBusy(true); setError("");
    try {
      await api.patch(`/support-emails/${editing.id}/`,
        { email: editing.email, owner_name: editing.owner_name });
      setEditing(null);
      load();
    } catch (err) {
      setError(err.data?.email?.[0] || err.message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleActive(r) {
    await api.patch(`/support-emails/${r.id}/`, { is_active: !r.is_active });
    load();
  }

  async function makePrimary(r) {
    await api.patch(`/support-emails/${r.id}/`, { is_primary: true });
    load();
  }

  async function remove(r) {
    if (r.is_primary) { setError("Cannot delete the primary inbox. Set another as primary first."); return; }
    if (!window.confirm(`Delete ${r.email}?`)) return;
    await api.del(`/support-emails/${r.id}/`);
    load();
  }

  const primary = rows.find((r) => r.is_primary);
  const aliases = rows.filter((r) => !r.is_primary);

  return (
    <div>
      <div className="card">
        <h3>Add support email / alias</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          The Care Panel fetches ONLY the primary inbox. Aliases are used for sending replies, and
          emails from any of these addresses are never re-imported.
        </p>
        <form className="row" onSubmit={add} style={{ flexWrap: "wrap", gap: 8 }}>
          <input
            placeholder="email address"
            value={draft.email}
            onChange={(e) => setDraft({ ...draft, email: e.target.value })}
            style={{ width: 280 }}
          />
          <input
            placeholder="owner / employee (e.g. Chintan Dabhi)"
            value={draft.owner_name}
            onChange={(e) => setDraft({ ...draft, owner_name: e.target.value })}
            style={{ width: 240 }}
          />
          <label className="row" style={{ gap: 4, alignItems: "center" }}>
            <input
              type="checkbox"
              checked={draft.is_primary}
              onChange={(e) => setDraft({ ...draft, is_primary: e.target.checked })}
            />
            Primary inbox
          </label>
          <button className="btn primary" disabled={busy}>Add Email</button>
        </form>
        {error && <div className="error">{error}</div>}
        {ok && <div style={{ color: "#2e7d32", marginTop: 8 }}>{ok}</div>}
      </div>

      <div className="card">
        <h3>Support emails ({rows.length})</h3>
        <table>
          <thead>
            <tr>
              <th>Email</th>
              <th>Owner / Employee</th>
              <th>Role</th>
              <th>Active</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {[primary, ...aliases].filter(Boolean).map((r) => (
              <tr key={r.id}>
                <td>
                  {editing?.id === r.id ? (
                    <input value={editing.email}
                      onChange={(e) => setEditing({ ...editing, email: e.target.value })}
                      style={{ width: 240 }} />
                  ) : <code>{r.email}</code>}
                </td>
                <td>
                  {editing?.id === r.id ? (
                    <input value={editing.owner_name}
                      onChange={(e) => setEditing({ ...editing, owner_name: e.target.value })}
                      placeholder="owner / employee" style={{ width: 180 }} />
                  ) : (r.owner_name || "—")}
                </td>
                <td>
                  {r.is_primary
                    ? <span className="badge" style={{ background: "#e3f2fd", color: "#1565c0",
                        padding: "2px 8px", borderRadius: 4, fontSize: 12 }}>Primary</span>
                    : <span className="muted">Alias</span>}
                </td>
                <td>
                  <button className="btn" onClick={() => toggleActive(r)}>
                    {r.is_active ? "Active" : "Inactive"}
                  </button>
                </td>
                <td style={{ display: "flex", gap: 6 }}>
                  {editing?.id === r.id ? (
                    <>
                      <button className="btn primary" disabled={busy} onClick={saveEdit}>Save</button>
                      <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
                    </>
                  ) : (
                    <>
                      <button className="btn" onClick={() =>
                        setEditing({ id: r.id, email: r.email, owner_name: r.owner_name })}>
                        Edit</button>
                      {!r.is_primary &&
                        <button className="btn" onClick={() => makePrimary(r)}>Make primary</button>}
                      {!r.is_primary &&
                        <button className="btn" onClick={() => remove(r)}>Delete</button>}
                    </>
                  )}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={5} className="muted" style={{ textAlign: "center", padding: 18 }}>
                  No support emails configured.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
