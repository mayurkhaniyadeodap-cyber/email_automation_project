import { useEffect, useState } from "react";
import { api } from "../api";
import { useScope } from "../scope.jsx";

function Metric({ n, label }) {
  return (
    <div className="metric">
      <div className="n">{n ?? "—"}</div>
      <div className="l">{label}</div>
    </div>
  );
}

function pct(rate) {
  return rate == null ? "—" : `${(rate * 100).toFixed(0)}%`;
}

export default function Analytics() {
  const { orgId, brandId } = useScope();
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!brandId) return;
    setError("");
    api
      .get("/analytics/overview/", { organization: orgId, brand: brandId })
      .then(setData)
      .catch((err) => setError(err.message));
  }, [orgId, brandId]);

  if (error) return <div className="error">{error}</div>;
  if (!data) return <div className="muted">Loading…</div>;

  const { volume, sla, ai, agents } = data;

  return (
    <div>
      <div className="card">
        <h3>Volume</h3>
        <div className="metrics">
          <Metric n={volume.total} label="Total tickets" />
          <Metric n={volume.open} label="Open" />
          <Metric n={volume.auto_resolved} label="Auto-resolved" />
          <Metric n={volume.ignored} label="Ignored" />
        </div>
      </div>

      <div className="card">
        <h3>SLA</h3>
        <div className="metrics">
          <Metric n={sla.breached} label="Breached (open)" />
          <Metric n={sla.due_soon} label="Due soon" />
          <Metric n={sla.met} label="Met" />
          <Metric n={sla.missed} label="Missed" />
          <Metric n={pct(sla.compliance_rate)} label="Compliance" />
        </div>
      </div>

      <div className="card">
        <h3>AI accuracy</h3>
        <div className="metrics">
          <Metric n={ai.classified} label="Classified" />
          <Metric n={ai.auto_handled} label="Auto-handled" />
          <Metric n={ai.uncategorized} label="Uncategorized" />
          <Metric n={ai.low_confidence} label="Low confidence" />
          <Metric n={ai.corrections} label="Corrections" />
          <Metric n={pct(ai.accuracy_rate)} label="Accuracy" />
          <Metric
            n={ai.avg_confidence != null ? ai.avg_confidence.toFixed(2) : "—"}
            label="Avg confidence"
          />
        </div>
      </div>

      <div className="card">
        <h3>Agent performance</h3>
        {Object.keys(agents).length === 0 ? (
          <span className="muted">No agent activity yet.</span>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Agent</th>
                <th>Total</th>
                <th>Replies</th>
                <th>Drafts</th>
                <th>Corrections</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(agents).map(([actor, c]) => (
                <tr key={actor}>
                  <td>{actor}</td>
                  <td>{c.total}</td>
                  <td>{c.reply_sent || 0}</td>
                  <td>{c.draft_created || 0}</td>
                  <td>{c.correction || 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
