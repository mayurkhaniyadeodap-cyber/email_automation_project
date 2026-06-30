import { useEffect, useState } from "react";
import { useNavigate, useOutletContext, useSearchParams } from "react-router-dom";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import Paper from "@mui/material/Paper";
import Tab from "@mui/material/Tab";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tabs from "@mui/material/Tabs";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import { api } from "../api";
import { StatusChip, fmtDate } from "./chips.jsx";

const TABS = [
  ["all", "ALL", { ignored: "false" }],
  ["classified", "CLASSIFIED", { ignored: "false", status: "classified" }],
  ["ticketed", "TICKETED", { ignored: "false", status: "awaiting_agent" }],
  ["ignored", "IGNORED", { ignored: "true" }],
];

export default function Inbox() {
  const { refreshKey, orgId, brandId } = useOutletContext();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  // A card may deep-link to a tab, e.g. /inbox?status=ignored.
  const initialTab = params.get("status") === "ignored" ? 3 : 0;
  const [tab, setTab] = useState(initialTab);
  const [search, setSearch] = useState("");
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const isIgnoredTab = TABS[tab][0] === "ignored";

  // Un-ignore (un-block) a wrongly-filtered email straight from the Ignored list:
  // restores it to the queue. stopPropagation so the row's open-detail click doesn't fire.
  async function unignore(id, e) {
    e.stopPropagation();
    try {
      await api.post(`/tickets/${id}/unignore/`);
    } finally {
      load();   // it leaves the Ignored tab, so refresh the list
    }
  }

  useEffect(() => { setTab(params.get("status") === "ignored" ? 3 : 0); },
    [params]); // react to card navigation

  async function load() {
    if (!brandId) return;
    setLoading(true);
    try {
      const scope = { organization: orgId, brand: brandId, search };
      const data = await api.get("/tickets/", { ...scope, ...TABS[tab][2] });
      let list = (data.results || data).map((t) => ({ ...t, _kind: "ticket" }));
      // The ALL tab also surfaces HELD conversations (no ticket yet -- verification
      // failed / awaiting evidence) so a fetched email is never invisible. Read-only;
      // opening one shows the held email, and it is NOT pushed to the Care Panel.
      if (TABS[tab][0] === "all") {
        const pend = await api.get("/pending/", scope);
        const held = (pend.results || pend).map((p) => ({
          id: `p${p.id}`, _kind: "pending", _pid: p.id,
          customer_email: p.customer_email, subject: p.subject,
          sub_topic: p.sub_topic, category: p.category,
          status: p.status || "awaiting_evidence", status_display: "Held — no ticket",
          ai_confidence: null, created_at: p.created_at,
        }));
        list = [...held, ...list].sort(
          (a, b) => new Date(b.created_at) - new Date(a.created_at));
      }
      setRows(list);
      setCount(list.length);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId, brandId, tab, refreshKey]);

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 2 }}>
        {tab === 3 ? "Ignored" : "Inbox"} ({count})
      </Typography>

      <Box sx={{ display: "flex", alignItems: "center", gap: 2, mb: 2, flexWrap: "wrap" }}>
        <Card sx={{ flexShrink: 0 }}>
          <Tabs value={tab} onChange={(e, v) => setTab(v)}>
            {TABS.map(([k, label]) => <Tab key={k} label={label} />)}
          </Tabs>
        </Card>
        <TextField
          size="small" placeholder="Search subject / sender…"
          value={search} onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && load()}
          sx={{ minWidth: 280 }}
        />
      </Box>

      <TableContainer component={Paper} variant="outlined">
        <Table>
          <TableHead>
            <TableRow>
              <TableCell>From</TableCell>
              <TableCell>Subject</TableCell>
              <TableCell>Category</TableCell>
              <TableCell>Status</TableCell>
              <TableCell>Conf.</TableCell>
              <TableCell>Received</TableCell>
              {isIgnoredTab && <TableCell align="right">Action</TableCell>}
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((t) => (
              <TableRow
                key={t.id} hover sx={{ cursor: "pointer" }}
                onClick={() => navigate(
                  t._kind === "pending" ? `/pending/${t._pid}` : `/tickets/${t.id}`)}
              >
                <TableCell>{t.customer_email || "—"}</TableCell>
                <TableCell>{t.subject || "—"}</TableCell>
                <TableCell>{t.sub_topic || t.category || "—"}</TableCell>
                <TableCell><StatusChip status={t.status} label={t.status_display} /></TableCell>
                <TableCell>{t.ai_confidence != null ? t.ai_confidence.toFixed(2) : "—"}</TableCell>
                <TableCell>{fmtDate(t.created_at)}</TableCell>
                {isIgnoredTab && (
                  <TableCell align="right">
                    <Button size="small" variant="outlined"
                      onClick={(e) => unignore(t.id, e)}>
                      Un-ignore
                    </Button>
                  </TableCell>
                )}
              </TableRow>
            ))}
            {!loading && rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={isIgnoredTab ? 7 : 6} align="center" sx={{ py: 5, color: "text.secondary" }}>
                  No emails. Click “Fetch Mail”, or run manage.py seed_demo.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  );
}
