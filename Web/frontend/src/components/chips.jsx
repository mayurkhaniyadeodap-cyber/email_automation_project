import Chip from "@mui/material/Chip";

const STATUS_COLOR = {
  new: "default",
  classified: "info",
  auto_resolved: "success",
  resolved: "success",
  closed: "success",
  awaiting_evidence: "warning",
  awaiting_agent: "warning",
  in_progress: "info",
  escalated: "error",
  ignored: "default",
};
const PRIORITY_COLOR = { high: "error", normal: "info", low: "default" };

export function StatusChip({ status, label }) {
  return (
    <Chip
      size="small"
      label={label || status || "—"}
      color={STATUS_COLOR[status] || "default"}
      variant={status === "new" ? "outlined" : "filled"}
    />
  );
}

export function PriorityChip({ priority, label }) {
  return (
    <Chip size="small" label={label || priority} color={PRIORITY_COLOR[priority] || "default"} />
  );
}

export function fmtDate(value) {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}
