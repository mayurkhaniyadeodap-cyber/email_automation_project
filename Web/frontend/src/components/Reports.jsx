import { useEffect, useState } from "react";
import { useNavigate, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import { api, getToken } from "../api";
import { fmtDate } from "./chips.jsx";

const REPORTS = {
  manual: {
    title: "Manual Reply Report", endpoint: "manual-replies",
    cols: [["date", "Date", fmtDate], ["employee_name", "Employee"],
           ["sender_email", "Sent From"],
           ["customer", "Customer"], ["subject", "Subject"], ["ticket", "Ticket"],
           ["reply_time", "Reply Time", fmtDate], ["attachments", "Attachments"], ["status", "Status"]],
  },
  auto: {
    title: "Auto Reply Report", endpoint: "auto-replies",
    cols: [["date", "Date", fmtDate], ["customer", "Customer"], ["subject", "Subject"],
           ["template", "Template"], ["auto_reply_time", "Auto Reply Time", fmtDate],
           ["ticket", "Ticket ID"], ["status", "Status"]],
  },
  login: {
    title: "Employee Login History", endpoint: "login-history",
    cols: [["employee", "Employee"], ["login_at", "Login", fmtDate], ["logout_at", "Logout", fmtDate],
           ["session_seconds", "Session (s)"], ["ip_address", "IP"], ["device", "Device"],
           ["browser", "Browser"]],
  },
};

const RANGES = [["", "All time"], ["today", "Today"], ["yesterday", "Yesterday"],
                ["7d", "Last 7 Days"], ["30d", "Last 30 Days"], ["custom", "Custom Date"]];

export default function Reports() {
  const { kind = "manual" } = useParams();
  const cfg = REPORTS[kind] || REPORTS.manual;
  const { orgId, brandId, refreshKey } = useOutletContext();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const employee = params.get("employee") || "";
  const [range, setRange] = useState("7d");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);

  function query() {
    const q = { organization: orgId, brand: brandId };
    if (range && range !== "custom") q.range = range;
    if (range === "custom") { if (since) q.since = since; if (until) q.until = until; }
    if (employee) q.employee = employee;
    return q;
  }

  async function load() {
    if (!brandId) return;
    const res = await api.get(`/analytics/${cfg.endpoint}/`, query());
    setRows(res.results || []); setCount(res.count ?? (res.results || []).length);
  }

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [kind, orgId, brandId, range, employee, refreshKey]);

  function exportAs(fmt) {
    const q = new URLSearchParams({ ...query(), export: fmt }).toString();
    // token via query so the browser download carries auth
    const url = `/api/analytics/${cfg.endpoint}/?${q}`;
    fetch(url, { headers: { Authorization: `Token ${getToken()}` } })
      .then((r) => r.blob()).then((b) => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(b);
        a.download = `${cfg.endpoint}.${fmt === "xlsx" ? "xlsx" : fmt}`;
        a.click(); URL.revokeObjectURL(a.href);
      });
  }

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 2 }}>
        {cfg.title} ({count}){employee ? ` — ${employee}` : ""}
      </Typography>
      <Stack direction="row" spacing={1} sx={{ mb: 2, alignItems: "center", flexWrap: "wrap" }}>
        <TextField select size="small" label="Range" value={range} sx={{ minWidth: 160 }}
          onChange={(e) => setRange(e.target.value)}>
          {RANGES.map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
        </TextField>
        {range === "custom" && <>
          <TextField type="date" size="small" label="From" InputLabelProps={{ shrink: true }}
            value={since} onChange={(e) => setSince(e.target.value)} />
          <TextField type="date" size="small" label="To" InputLabelProps={{ shrink: true }}
            value={until} onChange={(e) => setUntil(e.target.value)} />
          <Button variant="outlined" onClick={load}>Apply</Button>
        </>}
        <Box sx={{ flexGrow: 1 }} />
        <Button variant="outlined" onClick={() => exportAs("csv")}>CSV</Button>
        <Button variant="outlined" onClick={() => exportAs("xlsx")}>Excel</Button>
        <Button variant="outlined" onClick={() => exportAs("pdf")}>PDF</Button>
      </Stack>

      <Paper variant="outlined" sx={{ overflowX: "auto" }}>
        <Table size="small">
          <TableHead><TableRow>{cfg.cols.map(([k, l]) => <TableCell key={k}>{l}</TableCell>)}</TableRow></TableHead>
          <TableBody>
            {rows.map((r, i) => {
              const target = r.ticket_pk ? `/tickets/${r.ticket_pk}`
                : r.escalation_id ? `/escalations?open=${r.escalation_id}` : null;
              return (
                <TableRow key={i} hover sx={{ cursor: target ? "pointer" : "default" }}
                  onClick={() => target && navigate(target)}>
                  {cfg.cols.map(([k, , fmt]) => (
                    <TableCell key={k}>{fmt ? (r[k] ? fmt(r[k]) : "—") : (r[k] ?? "—")}</TableCell>
                  ))}
                </TableRow>
              );
            })}
            {rows.length === 0 && (
              <TableRow><TableCell colSpan={cfg.cols.length} align="center"
                sx={{ py: 4, color: "text.secondary" }}>No records.</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </Paper>
    </Box>
  );
}
