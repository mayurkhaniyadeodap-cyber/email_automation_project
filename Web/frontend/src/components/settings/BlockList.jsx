import { useEffect, useState } from "react";
import { api } from "../../api";
import { useScope } from "../../scope.jsx";

const KINDS = [
  ["sender_email", "Sender (exact email)"],
  ["sender_domain", "Domain (e.g. *@newsletter.xyz)"],
  ["marketing", "Marketing / bulk header"],
  ["noreply", "No-reply / automated"],
  ["internal", "Internal address"],
  ["spam", "Spam / phishing token"],
];

export default function BlockList() {
  const { orgId, brandId } = useScope();
  const [entries, setEntries] = useState([]);
  const [draft, setDraft] = useState({ kind: "sender_email", value: "", note: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    if (!brandId) return;
    setError("");
    try {
      const data = await api.get("/block-list/", { organization: orgId, brand: brandId });
      setEntries(data.results || data);
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
    if (!draft.value.trim()) return;
    setBusy(true);
    setError("");
    try {
      await api.post("/block-list/", { brand: Number(brandId), ...draft });
      setDraft({ kind: draft.kind, value: "", note: "" });
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function toggle(entry) {
    await api.patch(`/block-list/${entry.id}/`, { is_active: !entry.is_active });
    load();
  }

  async function remove(entry) {
    await api.del(`/block-list/${entry.id}/`);
    load();
  }

  return (
    <div>
      <div className="card">
        <h3>Add block rule</h3>
        <form className="row" onSubmit={add} style={{ flexWrap: "wrap", gap: 8 }}>
          <select
            value={draft.kind}
            onChange={(e) => setDraft({ ...draft, kind: e.target.value })}
          >
            {KINDS.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
          <input
            placeholder="value (email / domain / header token)"
            value={draft.value}
            onChange={(e) => setDraft({ ...draft, value: e.target.value })}
            style={{ width: 280 }}
          />
          <input
            placeholder="note (optional)"
            value={draft.note}
            onChange={(e) => setDraft({ ...draft, note: e.target.value })}
            style={{ width: 200 }}
          />
          <button className="btn primary" disabled={busy}>
            Add
          </button>
        </form>
        {error && <div className="error">{error}</div>}
      </div>

      <div className="card">
        <h3>Block list ({entries.length})</h3>
        <table>
          <thead>
            <tr>
              <th>Kind</th>
              <th>Value</th>
              <th>Note</th>
              <th>Active</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.id}>
                <td>{e.kind_display}</td>
                <td><code>{e.value}</code></td>
                <td className="muted">{e.note || "—"}</td>
                <td>
                  <span style={{ color: e.is_active ? "#d32f2f" : "#888", fontWeight: 600 }}>
                    {e.is_active ? "Blocking" : "Inactive"}
                  </span>
                </td>
                <td>
                  <button className="btn" onClick={() => toggle(e)}
                    style={{ marginRight: 8 }}>
                    {e.is_active ? "Unblock" : "Block"}
                  </button>
                  <button className="btn" onClick={() => remove(e)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
            {entries.length === 0 && (
              <tr>
                <td colSpan={5} className="muted" style={{ textAlign: "center", padding: 18 }}>
                  No block rules.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
