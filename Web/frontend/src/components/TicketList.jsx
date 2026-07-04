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
import TableSortLabel from "@mui/material/TableSortLabel";
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
// Date-range presets. today/yesterday/7d/30d map to the backend `range` param (server computes
// them in its own timezone); month presets + custom send explicit since/until; all -> no filter.
const RANGES = [
  ["all", "All Time"],
  ["today", "Today"],
  ["yesterday", "Yesterday"],
  ["7d", "Last 7 Days"],
  ["30d", "Last 30 Days"],
  ["this_month", "This Month"],
  ["last_month", "Last Month"],
  ["custom", "Custom Date"],
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

const _pad = (n) => String(n).padStart(2, "0");
const _iso = (d) => `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())}`;

// Translate a range preset (+ custom dates) into the query params the ticket API understands.
function rangeParams(range, since, until) {
  const now = new Date();
  switch (range) {
    case "today":
    case "yesterday":
    case "7d":
    case "30d":
      return { range };
    case "this_month":
      return { since: _iso(new Date(now.getFullYear(), now.getMonth(), 1)), until: _iso(now) };
    case "last_month":
      return {
        since: _iso(new Date(now.getFullYear(), now.getMonth() - 1, 1)),
        until: _iso(new Date(now.getFullYear(), now.getMonth(), 0)),
      };
    case "custom":
      return { since, until };
    default:
      return {}; // all time
  }
}

// "2026-07-03T10:25:..." -> { d: "03 Jul 2026", t: "10:25 AM" }
function fmtDate(iso) {
  if (!iso) return { d: "—", t: "" };
  const dt = new Date(iso);
  if (isNaN(dt)) return { d: "—", t: "" };
  return {
    d: dt.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }),
    t: dt.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" }),
  };
}

// Sortable columns -> ordering field (must be in the API's ordering_fields).
const SORTABLE = { ticket_id: "Ticket No.", created_at: "Date", priority: "Priority", status: "Status" };

export default function TicketList() {
  const { refreshKey, orgId, brandId } = useOutletContext();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || "";
  const priority = params.get("priority") || "";
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [range, setRange] = useState("all");
  const [since, setSince] = useState("");           // custom "From" input
  const [until, setUntil] = useState("");           // custom "To" input
  const [appliedSince, setAppliedSince] = useState("");
  const [appliedUntil, setAppliedUntil] = useState("");
  const [sortField, setSortField] = useState("created_at");
  const [sortDir, setSortDir] = useState("desc");   // newest first
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);
  const [page, setPage] = useState(1);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(false);

  const ordering = `${sortDir === "desc" ? "-" : ""}${sortField}`;

  function setStatus(s) {
    setPage(1);
    // Preserve a priority filter coming from the dashboard (e.g. High Priority card).
    const next = {};
    if (s) next.status = s;
    if (priority) next.priority = priority;
    setParams(next);
  }

  function changeRange(r) {
    setRange(r);
    setPage(1);
    if (r !== "custom") {                 // presets apply immediately; custom waits for Apply
      setAppliedSince("");
      setAppliedUntil("");
    }
  }

  function applyCustom() {
    setAppliedSince(since);
    setAppliedUntil(until);
    setPage(1);
  }

  function runSearch() {
    setAppliedSearch(search);
    setPage(1);
  }

  function sortBy(field) {
    setPage(1);
    if (sortField === field) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortField(field);
      setSortDir("asc");
    }
  }

  function queryParams(p) {
    return {
      organization: orgId, brand: brandId, status, priority,
      search: appliedSearch, ordering, page: p,
      ...rangeParams(range, appliedSince, appliedUntil),
    };
  }

  async function load() {
    if (!brandId) return;
    setLoading(true);
    try {
      const data = await api.get("/tickets/", queryParams(page));
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
  }, [orgId, brandId, status, priority, page, refreshKey,
      appliedSearch, range, appliedSince, appliedUntil, sortField, sortDir]);

  async function exportCsv() {
    const all = [];
    let p = 1, more = true;
    while (more) {
      const data = await api.get("/tickets/", queryParams(p));
      all.push(...(data.results || data));
      more = !!data.next; p += 1;
      if (p > 200) break;
    }
    const cols = [
      ["Ticket No.", (t) => t.ticket_number],
      ["Date", (t) => { const { d, t: tm } = fmtDate(t.created_at); return `${d} ${tm}`.trim(); }],
      ["Subject", (t) => t.subject],
      ["Customer", (t) => t.customer_name],
      ["Email", (t) => t.customer_email],
      ["Order No.", (t) => t.order_id],
      ["Category", (t) => t.sub_topic || t.category],
      ["Priority", (t) => t.priority_display || t.priority],
      ["Status", (t) => t.status_display || t.status],
    ];
    const esc = (x) => `"${String(x ?? "").replace(/"/g, '""')}"`;
    const csv = [
      cols.map(([h]) => esc(h)).join(","),
      ...all.map((t) => cols.map(([, f]) => esc(f(t))).join(",")),
    ].join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = `tickets-${status || "all"}.csv`; a.click();
    URL.revokeObjectURL(url);
  }

  const headCell = (field, label) => (
    <TableCell sortDirection={sortField === field ? sortDir : false}>
      <TableSortLabel active={sortField === field}
                      direction={sortField === field ? sortDir : "asc"}
                      onClick={() => sortBy(field)}>
        {label}
      </TableSortLabel>
    </TableCell>
  );

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 2 }}>
        {priority ? `${priority[0].toUpperCase()}${priority.slice(1)} Priority` : labelFor(status)} ({count})
      </Typography>

      <Box sx={{ display: "flex", gap: 2, mb: 2, flexWrap: "wrap", alignItems: "center" }}>
        <TextField select size="small" label="Status"
                   value={STATUSES.some(([v]) => v === status) ? status : ""}
                   onChange={(e) => setStatus(e.target.value)} sx={{ minWidth: 170 }}>
          {STATUSES.map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
        </TextField>
        <TextField select size="small" label="Range" value={range}
                   onChange={(e) => changeRange(e.target.value)} sx={{ minWidth: 150 }}>
          {RANGES.map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
        </TextField>
        {range === "custom" && (
          <>
            <TextField size="small" type="date" label="From" value={since}
                       onChange={(e) => setSince(e.target.value)}
                       InputLabelProps={{ shrink: true }} sx={{ minWidth: 150 }} />
            <TextField size="small" type="date" label="To" value={until}
                       onChange={(e) => setUntil(e.target.value)}
                       InputLabelProps={{ shrink: true }} sx={{ minWidth: 150 }} />
            <Button variant="contained" onClick={applyCustom}
                    disabled={!since && !until}>Apply</Button>
          </>
        )}
        <TextField size="small"
                   placeholder="Search Ticket No. / Customer / Email / Subject / Order No."
                   value={search} onChange={(e) => setSearch(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && runSearch()}
                   sx={{ minWidth: 340, flexGrow: 1 }} />
        <Button variant="outlined" onClick={runSearch}>Search</Button>
        <Box sx={{ flexGrow: 1 }} />
        <Button variant="outlined" startIcon={<DownloadIcon />} onClick={exportCsv}
                disabled={count === 0}>Export CSV</Button>
      </Box>

      <TableContainer component={Paper} variant="outlined" sx={{ overflowX: "auto" }}>
        <Table sx={{ minWidth: 900 }}>
          <TableHead>
            <TableRow>
              {headCell("ticket_id", "Ticket No.")}
              {headCell("created_at", "Date")}
              <TableCell>Subject</TableCell>
              <TableCell>Customer</TableCell>
              <TableCell>Category</TableCell>
              {headCell("priority", "Priority")}
              {headCell("status", "Status")}
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((t) => {
              const { d, t: tm } = fmtDate(t.created_at);
              return (
                <TableRow key={t.id} hover sx={{ cursor: "pointer" }}
                          onClick={() => navigate(`/tickets/${t.id}`)}>
                  {/* Care Panel (Gallabox) ticket number from store-json -- never the internal TKT id. */}
                  <TableCell sx={{ whiteSpace: "nowrap", fontWeight: 500 }}>{t.ticket_number || "—"}</TableCell>
                  <TableCell sx={{ whiteSpace: "nowrap" }}>
                    <div>{d}</div>
                    <div style={{ fontSize: 12, color: "#667" }}>{tm}</div>
                  </TableCell>
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
              );
            })}
            {!loading && rows.length === 0 && (
              <TableRow><TableCell colSpan={7} align="center"
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
