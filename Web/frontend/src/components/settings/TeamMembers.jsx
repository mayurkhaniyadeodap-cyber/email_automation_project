import { useEffect, useState } from "react";
import { api } from "../../api";

const ROLES = [
  ["admin", "Admin"],
  ["agent", "Agent"],
  ["viewer", "Viewer"],
];
const BLANK = { username: "", name: "", password: "", role: "agent" };

export default function TeamMembers() {
  const [members, setMembers] = useState([]);
  const [form, setForm] = useState(BLANK);
  const [editing, setEditing] = useState(null); // user id being edited
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");

  function load() {
    api.get("/users/")
      .then((d) => setMembers(d.results || d))
      .catch((e) => setError(e.data?.detail || e.message));
  }
  useEffect(load, []);

  function flash(setter, text) { setter(text); setTimeout(() => setter(""), 4000); }

  async function addMember(e) {
    e.preventDefault();
    setError("");
    if (!form.username.trim() || !form.password) {
      setError("Username and password are required."); return;
    }
    try {
      await api.post("/users/", form);
      setForm(BLANK);
      flash(setMsg, `Member "${form.username}" added.`);
      load();
    } catch (err) {
      setError(err.data?.username?.[0] || err.data?.detail || "Could not add member.");
    }
  }

  async function saveEdit(m) {
    try {
      await api.patch(`/users/${m.id}/`, { name: m.name, role: m.role });
      setEditing(null);
      flash(setMsg, "Member updated.");
      load();
    } catch (err) { setError(err.data?.detail || err.message); }
  }

  async function toggleActive(m) {
    await api.patch(`/users/${m.id}/`, { is_active: !m.is_active });
    flash(setMsg, m.is_active ? `Disabled ${m.username}.` : `Enabled ${m.username}.`);
    load();
  }

  async function resetPassword(m) {
    const pw = window.prompt(`New password for "${m.username}" (min 6 chars):`);
    if (!pw) return;
    try {
      await api.post(`/users/${m.id}/reset_password/`, { password: pw });
      flash(setMsg, `Password reset for ${m.username}.`);
    } catch (err) { setError(err.data?.detail || "Reset failed."); }
  }

  async function unlock(m) {
    await api.post(`/users/${m.id}/unlock/`);
    flash(setMsg, `Unlocked ${m.username}.`);
    load();
  }

  async function removeMember(m) {
    if (!window.confirm(`Permanently delete member "${m.username}"? This cannot be undone.`))
      return;
    try {
      await api.del(`/users/${m.id}/`);
      flash(setMsg, `Deleted ${m.username}.`);
      load();
    } catch (err) {
      setError(err.data?.detail || "Could not delete member.");
    }
  }

  return (
    <div>
      <div className="card">
        <h3>Add Member</h3>
        {error && <div className="error">{error}</div>}
        {msg && <div style={{ color: "var(--green)", marginBottom: 8 }}>{msg}</div>}
        <form className="row" style={{ flexWrap: "wrap", alignItems: "flex-end", gap: 12 }}
              onSubmit={addMember}>
          <div className="field" style={{ margin: 0 }}>
            <label>Username</label>
            <input value={form.username}
                   onChange={(e) => setForm({ ...form, username: e.target.value })} />
          </div>
          <div className="field" style={{ margin: 0 }}>
            <label>Full Name</label>
            <input value={form.name}
                   onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </div>
          <div className="field" style={{ margin: 0 }}>
            <label>Password</label>
            <input type="password" value={form.password}
                   onChange={(e) => setForm({ ...form, password: e.target.value })} />
          </div>
          <div className="field" style={{ margin: 0 }}>
            <label>Role</label>
            <select value={form.role}
                    onChange={(e) => setForm({ ...form, role: e.target.value })}>
              {ROLES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </div>
          <button className="btn primary" type="submit">Add Member</button>
        </form>
      </div>

      <div className="card">
        <h3>Team Members ({members.length})</h3>
        <table>
          <thead>
            <tr>
              <th>Username</th><th>Name</th><th>Role</th><th>Status</th>
              <th>Last login</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {members.map((m) => {
              const ed = editing === m.id;
              return (
                <tr key={m.id}>
                  <td><b>{m.username}</b></td>
                  <td>
                    {ed ? (
                      <input value={m.name}
                             onChange={(e) => setMembers(members.map(
                               (x) => x.id === m.id ? { ...x, name: e.target.value } : x))} />
                    ) : (m.name || "—")}
                  </td>
                  <td>
                    {ed ? (
                      <select value={m.role}
                              onChange={(e) => setMembers(members.map(
                                (x) => x.id === m.id ? { ...x, role: e.target.value } : x))}>
                        {ROLES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                      </select>
                    ) : (
                      <span className="badge">{m.role}</span>
                    )}
                  </td>
                  <td>
                    {m.is_locked
                      ? <span className="badge high">Locked</span>
                      : m.is_active
                        ? <span className="badge" style={{ color: "var(--green)" }}>Active</span>
                        : <span className="badge low">Disabled</span>}
                  </td>
                  <td className="muted" style={{ fontSize: 12 }}>
                    {m.last_login ? new Date(m.last_login).toLocaleString() : "never"}
                  </td>
                  <td>
                    <div className="row" style={{ gap: 6 }}>
                      {ed ? (
                        <button className="btn primary" onClick={() => saveEdit(m)}>Save</button>
                      ) : (
                        <button className="btn" onClick={() => setEditing(m.id)}>Edit</button>
                      )}
                      <button className="btn" onClick={() => toggleActive(m)}>
                        {m.is_active ? "Disable" : "Enable"}
                      </button>
                      <button className="btn" onClick={() => resetPassword(m)}>Reset PW</button>
                      {m.is_locked && (
                        <button className="btn" onClick={() => unlock(m)}>Unlock</button>
                      )}
                      <button className="btn" style={{ color: "var(--red)" }}
                              onClick={() => removeMember(m)}>Delete</button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
