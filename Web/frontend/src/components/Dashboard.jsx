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
import Sym from "./Sym.jsx";
import TicketTrendCard from "./TicketTrendCard.jsx";

const BLUE = "#2563eb", GREEN = "#16a34a", ORANGE = "#f59e0b", RED = "#ef4444";

const CARD = {
  p: 3, borderRadius: "16px", bgcolor: "#fff", border: "1px solid #eef1f5",
  boxShadow: "0 1px 2px rgba(16,24,40,.04), 0 1px 3px rgba(16,24,40,.06)",
  transition: "box-shadow .18s ease, transform .18s ease",
};

function SectionTitle({ children }) {
  return (
    <Typography sx={{ fontSize: 20, fontWeight: 700, color: "#0f172a", mb: 2 }}>{children}</Typography>
  );
}

function PanelTitle({ children }) {
  return (
    <Typography sx={{ fontSize: 16, fontWeight: 700, color: "#0f172a", mb: 2 }}>{children}</Typography>
  );
}

function KpiCard({ icon, title, count, subtitle, color, to }) {
  const navigate = useNavigate();
  return (
    <Paper
      elevation={0}
      onClick={() => to && navigate(to)}
      sx={{
        ...CARD, height: "100%", cursor: to ? "pointer" : "default",
        "&:hover": to ? { boxShadow: "0 8px 22px rgba(16,24,40,.10)", transform: "translateY(-2px)" } : {},
      }}
    >
      <Box sx={{
        width: 48, height: 48, borderRadius: "50%", bgcolor: `${color}15`, color,
        display: "grid", placeItems: "center", mb: 1.75,
      }}>
        <Sym name={icon} size={24} />
      </Box>
      <Typography sx={{ fontSize: 15, fontWeight: 600, color: "#475569" }}>{title}</Typography>
      <Typography sx={{ fontSize: 36, fontWeight: 800, color: "#0f172a", lineHeight: 1.1, mt: 0.5 }}>
        {count}
      </Typography>
      <Typography sx={{ fontSize: 13, color: "#94a3b8", mt: 0.5 }}>{subtitle}</Typography>
    </Paper>
  );
}

// Horizontal bar chart (monochrome blue) for ticket categories.
const BAR_SHADES = ["#2563eb", "#3b82f6", "#60a5fa", "#93c5fd", "#bfdbfe", "#c7d2fe"];
function CategoryBars({ data = [] }) {
  const clean = (data || []).map((x) => ({ label: String(x.label || "").replace(/^\d+\.?\s*/, ""), value: x.value }))
    .sort((a, b) => b.value - a.value);
  const top = clean.slice(0, 6);
  const total = clean.reduce((sm, x) => sm + x.value, 0) || 0;
  const max = Math.max(1, ...top.map((x) => x.value));
  if (top.length === 0) return <Typography sx={{ fontSize: 13, color: "#94a3b8" }}>No category data yet.</Typography>;
  return (
    <Box>
      {top.map((c, i) => (
        <Box key={i} sx={{ mb: i < top.length - 1 ? 2 : 0 }}>
          <Box sx={{ display: "flex", justifyContent: "space-between", mb: 0.75 }}>
            <Typography sx={{ fontSize: 13.5, color: "#334155", fontWeight: 600 }}>{c.label}</Typography>
            <Typography sx={{ fontSize: 13, color: "#64748b" }}>
              {c.value} ({total ? Math.round((c.value / total) * 100) : 0}%)
            </Typography>
          </Box>
          <Box sx={{ height: 10, bgcolor: "#eef1f5", borderRadius: "6px", overflow: "hidden" }}>
            <Box sx={{ height: "100%", width: `${(c.value / max) * 100}%`, bgcolor: BAR_SHADES[i % BAR_SHADES.length], borderRadius: "6px" }} />
          </Box>
        </Box>
      ))}
    </Box>
  );
}

function fmtDuration(sec) {
  if (sec == null) return "—";
  const s = Math.round(sec), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${s}s`;
}

const gridSix = { display: "grid", gap: 2.5, gridTemplateColumns: { xs: "1fr", sm: "repeat(3,1fr)", lg: "repeat(6,1fr)" } };
const gridFive = { display: "grid", gap: 2.5, gridTemplateColumns: { xs: "1fr", sm: "repeat(3,1fr)", lg: "repeat(5,1fr)" } };

export default function Dashboard() {
  const { refreshKey, orgId, brandId } = useOutletContext();
  const navigate = useNavigate();
  const [d, setD] = useState(null);
  const [loading, setLoading] = useState(true);
  const [composeCount, setComposeCount] = useState(null);

  useEffect(() => {
    if (!brandId) return;
    setLoading(true);
    api.get("/analytics/dashboard/", { organization: orgId, brand: brandId })
      .then(setD).finally(() => setLoading(false));
    api.get("/compose-emails/", { organization: orgId, brand: brandId })
      .then((r) => setComposeCount(r?.count ?? (Array.isArray(r) ? r.length : (r?.results || []).length)))
      .catch(() => setComposeCount(null));
  }, [orgId, brandId, refreshKey]);

  if (loading || !d) {
    return <Box sx={{ p: 6, textAlign: "center" }}><CircularProgress /></Box>;
  }

  const s = d.summary || {}, p = d.pipeline || {};
  const cnt = (x) => (x && typeof x === "object" ? (x.total ?? 0) : (x ?? 0));
  const tdy = (x) => (x && typeof x === "object" && x.today != null ? x.today : null);
  const card = (icon, title, src, color, to) => ({
    icon, title, color, to, count: cnt(src),
    subtitle: tdy(src) != null ? `Today +${tdy(src)}` : "Currently open",
  });

  const support = [
    card("mail", "Total Emails", s.total_emails, BLUE, "/inbox"),
    card("confirmation_number", "Total Tickets", s.total_tickets, BLUE, "/tickets"),
    card("folder_open", "Open Tickets", s.open_tickets, ORANGE, "/tickets?status=open"),
    card("task_alt", "Closed Tickets", s.closed_tickets, GREEN, "/tickets?status=closed"),
    card("block", "Ignored Emails", s.ignored_emails, ORANGE, "/inbox?status=ignored"),
    card("flag", "High Priority", s.high_priority, RED, "/tickets?priority=high"),
  ];

  const pipeline = [
    card("verified_user", "Waiting Verification", s.pending_manual_review, ORANGE, "/escalation"),
    card("photo_camera", "Waiting Evidence", p.waiting_evidence, ORANGE, "/pending?status=waiting_for_video"),
    card("support_agent", "Awaiting Agent", p.awaiting_agent, BLUE, "/tickets?status=awaiting_agent"),
    card("autorenew", "In Progress", p.in_progress, BLUE, "/tickets?status=in_progress,escalated"),
    card("check_circle", "Resolved", p.resolved, GREEN, "/tickets?status=resolved,auto_resolved"),
  ];

  const automation = [
    card("smart_toy", "Auto Replies Sent", s.auto_replies, GREEN, "/reports/auto"),
    card("reply", "Manual Reply Sent", s.manual_replies, BLUE, "/reports/manual"),
    card("verified_user", "Verification Emails", s.verification_emails, ORANGE, "/pending"),
    card("attach_file", "Evidence Requests", s.evidence_requests, ORANGE, "/pending?status=waiting_for_video"),
    { icon: "edit_square", title: "Compose Emails", color: BLUE, to: "/compose",
      count: composeCount ?? "—", subtitle: composeCount != null ? "Total" : "—" },
  ];

  const perf = d.employee_performance || [];

  return (
    <Box sx={{ maxWidth: 1360, pb: 1 }}>
      {/* 1) Support Overview */}
      <Box sx={{ mb: 4 }}>
        <SectionTitle>Support Overview</SectionTitle>
        <Box sx={gridSix}>{support.map((c) => <KpiCard key={c.title} {...c} />)}</Box>
      </Box>

      {/* 2) Ticket Pipeline */}
      <Box sx={{ mb: 4 }}>
        <SectionTitle>Ticket Pipeline</SectionTitle>
        <Box sx={gridFive}>{pipeline.map((c) => <KpiCard key={c.title} {...c} />)}</Box>
      </Box>

      {/* 3) Automation Overview */}
      <Box sx={{ mb: 4 }}>
        <SectionTitle>Automation Overview</SectionTitle>
        <Box sx={gridFive}>{automation.map((c) => <KpiCard key={c.title} {...c} />)}</Box>
      </Box>

      {/* 5) Ticket Trend (dynamic Week / Month / Year) */}
      <Box sx={{ mb: 4 }}>
        <TicketTrendCard orgId={orgId} brandId={brandId} refreshKey={refreshKey} />
      </Box>

      {/* 6) Ticket Categories */}
      <Box sx={{ mb: 4 }}>
        <Paper elevation={0} sx={CARD}>
          <PanelTitle>Ticket Categories</PanelTitle>
          <CategoryBars data={d.category_distribution} />
        </Paper>
      </Box>

      {/* 7) Employee Performance */}
      <Box sx={{ mb: 3 }}>
        <Paper elevation={0} sx={{ ...CARD, p: 0, overflow: "hidden" }}>
          <Box sx={{ p: 3, pb: 2 }}><PanelTitle>Employee Performance</PanelTitle></Box>
          <Box sx={{ overflowX: "auto" }}>
            <Table sx={{ minWidth: 820 }}>
              <TableHead>
                <TableRow sx={{
                  "& th": {
                    bgcolor: "#f8fafc", fontWeight: 700, color: "#64748b", fontSize: 12,
                    textTransform: "uppercase", letterSpacing: 0.4, borderBottom: "1px solid #eef1f5", py: 1.5,
                  },
                }}>
                  <TableCell>Employee</TableCell>
                  <TableCell align="right">Manual Replies</TableCell>
                  <TableCell align="right">Auto Replies</TableCell>
                  <TableCell align="right">Created Tickets</TableCell>
                  <TableCell align="right">Resolved Tickets</TableCell>
                  <TableCell align="right">Escalations</TableCell>
                  <TableCell align="right">Avg Response Time</TableCell>
                  <TableCell>Last Active</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {perf.map((e) => (
                  <TableRow
                    key={e.employee_key || e.employee_email}
                    hover
                    sx={{
                      cursor: "pointer", transition: "background .12s",
                      "&:hover": { bgcolor: "#f8fafc" },
                      "& td": { borderBottom: "1px solid #f1f5f9", py: 1.5, fontSize: 14 },
                    }}
                    onClick={() => (e.employee_key || e.employee_email) &&
                      navigate(`/reports/manual?employee=${encodeURIComponent(e.employee_key || e.employee_email)}`)}
                  >
                    <TableCell sx={{ fontWeight: 600, color: "#0f172a" }}>{e.employee_name || "—"}</TableCell>
                    <TableCell align="right">{e.manual_replies}</TableCell>
                    <TableCell align="right">{e.auto_replies}</TableCell>
                    <TableCell align="right">{e.tickets_created}</TableCell>
                    <TableCell align="right">{e.tickets_resolved}</TableCell>
                    <TableCell align="right">{e.escalations_handled}</TableCell>
                    <TableCell align="right">{fmtDuration(e.avg_response_seconds)}</TableCell>
                    <TableCell sx={{ color: "#64748b", fontSize: 13 }}>
                      {e.last_active ? fmtDate(e.last_active) : "—"}
                    </TableCell>
                  </TableRow>
                ))}
                {perf.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={8} align="center" sx={{ py: 4, color: "text.secondary" }}>
                      No agent activity yet.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </Box>
        </Paper>
      </Box>

      <Typography sx={{ textAlign: "center", color: "#94a3b8", fontSize: 12.5, py: 2 }}>
        © {new Date().getFullYear()} DeoDap Care Panel. All rights reserved.
      </Typography>
    </Box>
  );
}
