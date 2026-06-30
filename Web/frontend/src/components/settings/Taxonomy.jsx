import { useEffect, useState } from "react";
import { api } from "../../api";
import { useScope } from "../../scope.jsx";

const ACTIONS = [
  ["info_only", "Info only (auto-send)"],
  ["await_evidence", "Await evidence (auto-send template)"],
  ["create_ticket", "Create ticket (draft)"],
  ["update_system", "Update in system (agent)"],
  ["continue_check", "Continue to next check"],
  ["trigger_cancellation_refund_pickup", "Trigger cancel / refund / pickup"],
];

function RuleRow({ rule, reload }) {
  const [r, setR] = useState(rule);
  const [busy, setBusy] = useState(false);
  useEffect(() => setR(rule), [rule]);

  async function save() {
    setBusy(true);
    try {
      await api.patch(`/rules/${rule.id}/`, {
        condition: r.condition,
        then_response: r.then_response,
        action: r.action,
        is_active: r.is_active,
      });
      reload();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ borderTop: "1px solid var(--border)", padding: "8px 0" }}>
      <div className="field" style={{ marginBottom: 6 }}>
        <label>IF condition</label>
        <input value={r.condition} onChange={(e) => setR({ ...r, condition: e.target.value })} />
      </div>
      <div className="field" style={{ marginBottom: 6 }}>
        <label>THEN response</label>
        <textarea
          style={{ minHeight: 50 }}
          value={r.then_response}
          onChange={(e) => setR({ ...r, then_response: e.target.value })}
        />
      </div>
      <div className="row">
        <select value={r.action} onChange={(e) => setR({ ...r, action: e.target.value })}>
          {ACTIONS.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
        <label className="muted">
          <input
            type="checkbox"
            checked={r.is_active}
            onChange={(e) => setR({ ...r, is_active: e.target.checked })}
          />{" "}
          active
        </label>
        <button className="btn" onClick={save} disabled={busy}>
          {busy ? "…" : "Save"}
        </button>
      </div>
    </div>
  );
}

function TemplateRow({ tpl, reload }) {
  const [body, setBody] = useState(tpl.body);
  const [busy, setBusy] = useState(false);
  useEffect(() => setBody(tpl.body), [tpl]);

  async function save() {
    setBusy(true);
    try {
      await api.patch(`/templates/${tpl.id}/`, { body });
      reload();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ borderTop: "1px solid var(--border)", padding: "8px 0" }}>
      <div className="field" style={{ marginBottom: 6 }}>
        <label>Template “{tpl.name}”</label>
        <textarea style={{ minHeight: 50 }} value={body} onChange={(e) => setBody(e.target.value)} />
      </div>
      <button className="btn" onClick={save} disabled={busy}>
        {busy ? "…" : "Save"}
      </button>
    </div>
  );
}

function SubTopicCard({ sub, reload }) {
  const [open, setOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [newRule, setNewRule] = useState({ condition: "", then_response: "", action: "info_only" });

  async function addRule() {
    await api.post("/rules/", {
      sub_topic: sub.id,
      condition: newRule.condition,
      then_response: newRule.then_response,
      action: newRule.action,
      position: (sub.rules?.length || 0) + 1,
    });
    setNewRule({ condition: "", then_response: "", action: "info_only" });
    setAdding(false);
    reload();
  }

  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 10, marginBottom: 8 }}>
      <div className="row clickable" onClick={() => setOpen(!open)} style={{ cursor: "pointer" }}>
        <b>{sub.code} {sub.name}</b>
        {sub.is_sensitive && <span className="badge high">sensitive</span>}
        <span className="right muted">
          {sub.rules?.length || 0} rules · {sub.templates?.length || 0} templates
        </span>
      </div>
      {open && (
        <div style={{ marginTop: 8 }}>
          {(sub.rules || []).map((r) => (
            <RuleRow key={r.id} rule={r} reload={reload} />
          ))}
          {(sub.templates || []).map((t) => (
            <TemplateRow key={t.id} tpl={t} reload={reload} />
          ))}

          {adding ? (
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 8, marginTop: 8 }}>
              <div className="field" style={{ marginBottom: 6 }}>
                <label>New rule — IF condition</label>
                <input
                  value={newRule.condition}
                  onChange={(e) => setNewRule({ ...newRule, condition: e.target.value })}
                />
              </div>
              <div className="field" style={{ marginBottom: 6 }}>
                <label>THEN response</label>
                <textarea
                  style={{ minHeight: 50 }}
                  value={newRule.then_response}
                  onChange={(e) => setNewRule({ ...newRule, then_response: e.target.value })}
                />
              </div>
              <div className="row">
                <select
                  value={newRule.action}
                  onChange={(e) => setNewRule({ ...newRule, action: e.target.value })}
                >
                  {ACTIONS.map(([v, l]) => (
                    <option key={v} value={v}>
                      {l}
                    </option>
                  ))}
                </select>
                <button className="btn primary" onClick={addRule}>
                  Add rule
                </button>
                <button className="btn" onClick={() => setAdding(false)}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <button className="btn" style={{ marginTop: 8 }} onClick={() => setAdding(true)}>
              + Add rule
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default function Taxonomy() {
  const { orgId, brandId } = useScope();
  const [categories, setCategories] = useState([]);
  const [subsByCat, setSubsByCat] = useState({});
  const [openCat, setOpenCat] = useState(null);
  const [error, setError] = useState("");

  async function load() {
    if (!brandId) return;
    setError("");
    try {
      const cats = await api.get("/categories/", { organization: orgId, brand: brandId });
      const subs = await api.get("/sub-topics/", { organization: orgId, brand: brandId });
      const subList = subs.results || subs;
      const grouped = {};
      subList.forEach((s) => {
        (grouped[s.category] = grouped[s.category] || []).push(s);
      });
      setCategories(cats.results || cats);
      setSubsByCat(grouped);
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId, brandId]);

  return (
    <div className="card">
      <h3>Categories &amp; rules</h3>
      {error && <div className="error">{error}</div>}
      {categories.map((c) => {
        const subs = subsByCat[c.id] || [];
        const isOpen = openCat === c.id;
        return (
          <div key={c.id} style={{ marginBottom: 6 }}>
            <div
              className="row clickable"
              style={{ padding: "8px 4px", cursor: "pointer" }}
              onClick={() => setOpenCat(isOpen ? null : c.id)}
            >
              <b>{c.code}. {c.name}</b>
              <span className="right muted">{subs.length} sub-topics</span>
            </div>
            {isOpen && (
              <div style={{ paddingLeft: 12 }}>
                {subs.length === 0 && (
                  <div className="muted" style={{ padding: 6 }}>
                    No sub-topics seeded for this category.
                  </div>
                )}
                {subs.map((s) => (
                  <SubTopicCard key={s.id} sub={s} reload={load} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
