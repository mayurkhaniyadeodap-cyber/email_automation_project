import { useEffect, useState } from "react";
import { useNavigate, useOutletContext, useSearchParams } from "react-router-dom";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import DownloadIcon from "@mui/icons-material/Download";
import { api } from "../api";
import { PriorityChip, StatusChip } from "./chips.jsx";

const STATUSES = [
  ["", "All statuses"],
  ["open", "Open"],
  ["awaiting_evidence", "Awaiting Evidence"],
  ["awaiting_agent", "Awaiting Agent"],
  ["in_progress", "In Progress"],
  ["escalated", "Escalated"],
  ["resolved", "Resolved"],
  ["closed", "Closed"],
  ["auto_resolved", "Auto-Resolved"],
];
const LABELS = {
  "": "Tickets",
  open: "Open Tickets",
  awaiting_agent: "Awaiting Agent",
  awaiting_evidence: "Awaiting Evidence",
  "in_progress,escalated": "In Progress / Escalated",
  "resolved,closed,auto_resolved": "Resolved Tickets",
  resolved: "Resolved Tickets",
};
const labelFor = (s) =>
  LABELS[s] || (s ? s.replace(/,/g, " / ") + " Tickets" : "Tickets");

export default function TicketList() {
  const { refreshKey, orgId, brandId } = useOutletContext();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || "";
  const priority = params.get("priority") || "";
  const [search, setSearch] = useState("");
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);
  const [page, setPage] = useState(1);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(false);

  function setStatus(s) {
    setPage(1);
    // Preserve a priority filter coming from the dashboard (e.g. High Priority card).
    const next = {};
    if (s) next.status = s;
    if (priority) next.priority = priority;
    setParams(next);
  }

  async function load() {
    if (!brandId) return;
    setLoading(true);
    try {
      const data = await api.get("/tickets/", {
        organization: orgId, brand: brandId, status, priority, search, page,
      });
      setRows(data.results || data);
      setCount(data.count ?? (data.results || data).length);
      setHasNext(!!data.next);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId, brandId, status, priority, page, refreshKey]);

  async function exportCsv() {
    const all = [];
    let p = 1, more = true;
    while (more) {
      const data = await api.get("/tickets/", {
        organization: orgId, brand: brandId, status, priority, search, page: p });
      all.push(...(data.results || data));
      more = !!data.next; p += 1;
      if (p > 200) break;
    }
    const cols = ["ticket_id", "subject", "customer_email", "category", "priority", "status"];
    const esc = (x) => `"${String(x ?? "").replace(/"/g, '""')}"`;
    const csv = [cols.join(","), ...all.map((t) => cols.map((c) => esc(t[c])).join(","))].join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = `tickets-${status || "all"}.csv`; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 2 }}>
        {priority ? `${priority[0].toUpperCase()}${priority.slice(1)} Priority` : labelFor(status)} ({count})
      </Typography>

      <Box sx={{ display: "flex", gap: 2, mb: 2, flexWrap: "wrap", alignItems: "center" }}>
        <TextField select size="small" label="Status"
                   value={STATUSES.some(([v]) => v === status) ? status : ""}
                   onChange={(e) => setStatus(e.target.value)} sx={{ minWidth: 180 }}>
          {STATUSES.map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
        </TextField>
        <TextField size="small" placeholder="Search code / customer / subject…"
                   value={search} onChange={(e) => setSearch(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && (setPage(1), load())}
                   sx={{ minWidth: 280 }} />
        <Button variant="outlined" onClick={() => { setPage(1); load(); }}>Search</Button>
        <Box sx={{ flexGrow: 1 }} />
        <Button variant="outlined" startIcon={<DownloadIcon />} onClick={exportCsv}
                disabled={count === 0}>Export CSV</Button>
      </Box>

      <TableContainer component={Paper} variant="outlined">
        <Table>
          <TableHead>
            <TableRow>
              <TableCell>Code</TableCell><TableCell>Subject</TableCell>
              <TableCell>Customer</TableCell><TableCell>Category</TableCell>
              <TableCell>Priority</TableCell><TableCell>Status</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((t) => (
              <TableRow key={t.id} hover sx={{ cursor: "pointer" }}
                        onClick={() => navigate(`/tickets/${t.id}`)}>
                <TableCell>{t.ticket_id}</TableCell>
                <TableCell>{t.subject || "—"}</TableCell>
                {/* Customer = verified order owner name (never the sender); email beneath. */}
                <TableCell>
                  <div>{t.customer_name || "Unknown"}</div>
                  <div style={{ fontSize: 12, color: "#667" }}>{t.customer_email || "—"}</div>
                </TableCell>
                <TableCell>{t.sub_topic || t.category || "—"}</TableCell>
                <TableCell><PriorityChip priority={t.priority} label={t.priority_display} /></TableCell>
                <TableCell><StatusChip status={t.status} label={t.status_display} /></TableCell>
              </TableRow>
            ))}
            {!loading && rows.length === 0 && (
              <TableRow><TableCell colSpan={6} align="center"
                sx={{ py: 5, color: "text.secondary" }}>No tickets found.</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </TableContainer>

      <Box sx={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 2, mt: 2 }}>
        <Button size="small" disabled={page <= 1 || loading} onClick={() => setPage(page - 1)}>Prev</Button>
        <Typography variant="body2">Page {page}</Typography>
        <Button size="small" disabled={!hasNext || loading} onClick={() => setPage(page + 1)}>Next</Button>
      </Box>
    </Box>
  );
}
