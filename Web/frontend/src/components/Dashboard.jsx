import { useEffect, useState } from "react";
import { useNavigate, useOutletContext } from "react-router-dom";
import Box from "@mui/material/Box";
import CircularProgress from "@mui/material/CircularProgress";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";
import { api } from "../api";
import { fmtDate } from "./chips.jsx";

// Uniform, non-stretching card grid (auto-fill keeps every card the same width, even on a
// partial last row -- the old flexbox stretched 2-card rows to full width).
const GRID = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))",
  gap: 1.5,   // MUI spacing: 1.5 * 8px = 12px (NOT 1.5px)
  mb: 3,
};

function KPI({ label, data, color, to }) {
  const navigate = useNavigate();
  const total = data?.total ?? 0;
  const hasTrend = data?.today != null || data?.week != null;
  return (
    <Paper
      elevation={0}
      onClick={() => to && navigate(to)}
      sx={{
        position: "relative", overflow: "hidden", p: 2, pl: 2.25, borderRadius: 2,
        border: "1px solid", borderColor: "divider", cursor: to ? "pointer" : "default",
        transition: "box-shadow .15s, transform .15s",
        "&:hover": to ? { boxShadow: 3, transform: "translateY(-2px)" } : {},
        "&::before": {
          content: '""', position: "absolute", left: 0, top: 0, bottom: 0, width: 4, bgcolor: color,
        },
      }}
    >
      <Typography sx={{ fontSize: 30, fontWeight: 800, lineHeight: 1.1, color: "#1f2937" }}>
        {total}
      </Typography>
      <Typography variant="body2" sx={{ color: "text.secondary", mt: 0.5, fontWeight: 500 }}>
        {label}
      </Typography>
      {hasTrend && (
        <Typography variant="caption" sx={{ color: "text.disabled", display: "block", mt: 0.5 }}>
          {data?.today != null ? `Today ${data.today}` : ""}
          {data?.week != null ? `  ·  7d ${data.week}` : ""}
        </Typography>
      )}
    </Paper>
  );
}

export default function Dashboard() {
  const { refreshKey, orgId, brandId } = useOutletContext();
  const navigate = useNavigate();
  const [d, setD] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!brandId) return;
    setLoading(true);
    api.get("/analytics/dashboard/", { organization: orgId, brand: brandId })
      .then(setD).finally(() => setLoading(false));
  }, [orgId, brandId, refreshKey]);

  if (loading || !d) return <Box sx={{ p: 6, textAlign: "center" }}><CircularProgress /></Box>;
  const s = d.summary || {}, p = d.pipeline || {};

  const cards = [
    ["Total Emails Received", s.total_emails, "#37474f", "/inbox"],
    ["Total Tickets", s.total_tickets, "#1565c0", "/tickets"],
    ["Open Tickets", s.open_tickets, "#2e7d32", "/tickets?status=open"],
    ["Closed Tickets", s.closed_tickets, "#546e7a", "/tickets?status=closed"],
    ["Ignored Emails", s.ignored_emails, "#ed6c02", "/inbox?status=ignored"],
    ["High Priority", s.high_priority, "#d32f2f", "/tickets?priority=high"],
  ];

  const pipeline = [
    ["Waiting For Evidence", { total: p.waiting_evidence }, "#f9a825", "/pending?status=waiting_for_video"],
    ["Awaiting Agent", { total: p.awaiting_agent }, "#0288d1", "/tickets?status=awaiting_agent"],
    ["In Progress", { total: p.in_progress }, "#6a1b9a", "/tickets?status=in_progress,escalated"],
    ["Resolved", { total: p.resolved }, "#2e7d32", "/tickets?status=resolved,auto_resolved"],
    ["Auto Replies Sent", s.auto_replies, "#00838f", "/reports/auto"],
    ["Manual Replies Sent", s.manual_replies, "#6a1b9a", "/reports/manual"],
  ];

  return (
    <Box sx={{ maxWidth: 1280 }}>
      <SectionTitle>Support Overview</SectionTitle>
      <Box sx={GRID}>
        {cards.map(([label, data, color, to]) => (
          <KPI key={label} label={label} data={data} color={color} to={to} />
        ))}
      </Box>

      <SectionTitle>Pipeline</SectionTitle>
      <Box sx={GRID}>
        {pipeline.map(([label, data, color, to]) => (
          <KPI key={label} label={label} data={data} color={color} to={to} />
        ))}
      </Box>

      {/* Employee performance */}
      <SectionTitle>Employee Performance</SectionTitle>
      <Paper elevation={0} sx={{ overflowX: "auto", borderRadius: 2, border: "1px solid",
        borderColor: "divider" }}>
        <Table size="small">
          <TableHead>
            <TableRow sx={{ "& th": { bgcolor: "#f8fafc", fontWeight: 700, color: "#475569" } }}>
              <TableCell>Employee</TableCell><TableCell>Email</TableCell>
              <TableCell align="right">Manual</TableCell><TableCell align="right">Auto</TableCell>
              <TableCell align="right">Created</TableCell><TableCell align="right">Resolved</TableCell>
              <TableCell align="right">Escalations</TableCell><TableCell align="right">Avg Resp</TableCell>
              <TableCell>Last Active</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {(d.employee_performance || []).map((e) => (
              <TableRow key={e.employee_key || e.employee_email} hover sx={{ cursor: "pointer" }}
                onClick={() => (e.employee_key || e.employee_email) &&
                  navigate(`/reports/manual?employee=${encodeURIComponent(e.employee_key || e.employee_email)}`)}>
                <TableCell>{e.employee_name || "—"}</TableCell>
                <TableCell>{e.employee_email || "—"}</TableCell>
                <TableCell align="right">{e.manual_replies}</TableCell>
                <TableCell align="right">{e.auto_replies}</TableCell>
                <TableCell align="right">{e.tickets_created}</TableCell>
                <TableCell align="right">{e.tickets_resolved}</TableCell>
                <TableCell align="right">{e.escalations_handled}</TableCell>
                <TableCell align="right">{e.avg_response_seconds != null ? `${e.avg_response_seconds}s` : "—"}</TableCell>
                <TableCell>{e.last_active ? fmtDate(e.last_active) : "—"}</TableCell>
              </TableRow>
            ))}
            {(d.employee_performance || []).length === 0 && (
              <TableRow><TableCell colSpan={9} align="center" sx={{ py: 3, color: "text.secondary" }}>
                No agent activity yet.</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </Paper>
    </Box>
  );
}

function SectionTitle({ children }) {
  return (
    <Typography sx={{ fontSize: 13, fontWeight: 700, letterSpacing: 0.6, textTransform: "uppercase",
      color: "#64748b", mb: 1.25, mt: 0.5 }}>
      {children}
    </Typography>
  );
}
