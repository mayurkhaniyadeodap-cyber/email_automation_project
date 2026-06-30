import { useEffect, useState } from "react";
import { useNavigate, useOutletContext, useSearchParams } from "react-router-dom";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
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
import { fmtDate } from "./chips.jsx";

export default function Pending() {
  const { refreshKey, orgId, brandId } = useOutletContext();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const status = params.get("status") || "";
  const [search, setSearch] = useState("");
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(false);

  async function load() {
    if (!brandId) return;
    setLoading(true);
    try {
      const scope = { organization: orgId, brand: brandId, search };
      // Held conversations (no ticket) + tickets already created but awaiting evidence.
      const [pend, tix] = await Promise.all([
        api.get("/pending/", scope),
        api.get("/tickets/", { ...scope, status: "awaiting_evidence" }),
      ]);
      const held = (pend.results || pend).map((r) => ({
        key: `p${r.id}`, customer_email: r.customer_email, order_id: r.order_id,
        phone: r.phone, issue: r.issue_summary || r.category || r.subject,
        status: r.status || "waiting_for_video", requests: r.evidence_requests,
        created_at: r.created_at, ticketId: null, pendingId: r.id,
      }));
      const tickets = (tix.results || tix).map((t) => ({
        key: `t${t.id}`, customer_email: t.customer_email, order_id: t.order_id || "",
        phone: t.phone || "", issue: t.sub_topic || t.subject || t.category,
        status: t.status, requests: t.evidence_requests ?? "",
        created_at: t.created_at, ticketId: t.id,
      }));
      const all = [...held, ...tickets];
      setRows(all);
      setCount(all.length);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId, brandId, status, refreshKey]);

  function exportCsv() {
    const cols = ["customer_email", "phone", "order_id", "issue", "status",
                  "requests", "created_at"];
    const esc = (x) => `"${String(x ?? "").replace(/"/g, '""')}"`;
    const csv = [cols.join(","), ...rows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = "pending-conversations.csv"; a.click();
    URL.revokeObjectURL(url);
  }

  const title = status === "waiting_for_video" ? "Waiting for Video" : "Waiting for Evidence / Video";

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 2 }}>{title} ({count})</Typography>

      <Box sx={{ display: "flex", gap: 2, mb: 2, alignItems: "center" }}>
        <TextField size="small" placeholder="Search email / order / phone…"
                   value={search} onChange={(e) => setSearch(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && load()} sx={{ minWidth: 280 }} />
        <Button variant="outlined" onClick={load}>Search</Button>
        <Box sx={{ flexGrow: 1 }} />
        <Button variant="outlined" startIcon={<DownloadIcon />} onClick={exportCsv}
                disabled={count === 0}>Export CSV</Button>
      </Box>

      <TableContainer component={Paper} variant="outlined">
        <Table>
          <TableHead>
            <TableRow>
              <TableCell>Customer</TableCell><TableCell>Order ID</TableCell>
              <TableCell>Phone</TableCell><TableCell>Issue</TableCell>
              <TableCell>Status</TableCell><TableCell>Requests</TableCell>
              <TableCell>Since</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={r.key} hover sx={{ cursor: "pointer" }}
                        onClick={() => navigate(
                          r.ticketId ? `/tickets/${r.ticketId}` : `/pending/${r.pendingId}`)}>
                <TableCell>{r.customer_email || "—"}</TableCell>
                <TableCell>{r.order_id || "—"}</TableCell>
                <TableCell>{r.phone || "—"}</TableCell>
                <TableCell>{r.issue || "—"}</TableCell>
                <TableCell><span className="badge awaiting_evidence">{r.status}</span></TableCell>
                <TableCell>{r.requests}</TableCell>
                <TableCell>{fmtDate(r.created_at)}</TableCell>
              </TableRow>
            ))}
            {!loading && rows.length === 0 && (
              <TableRow><TableCell colSpan={7} align="center"
                sx={{ py: 5, color: "text.secondary" }}>
                No conversations waiting for evidence.</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  );
}
