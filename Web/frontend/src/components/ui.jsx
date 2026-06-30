// Small shared presentational helpers.

export function Badge({ kind, children }) {
  return <span className={`badge ${kind || ""}`}>{children}</span>;
}

export function StatusBadge({ status, label }) {
  return <Badge kind={status}>{label || status}</Badge>;
}

export function PriorityBadge({ priority, label }) {
  return <Badge kind={priority}>{label || priority}</Badge>;
}

export function fmtDate(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString();
}
