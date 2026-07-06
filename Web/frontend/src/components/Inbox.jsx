import { useEffect, useState } from "react";
import { useNavigate, useOutletContext, useSearchParams } from "react-router-dom";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import FormControl from "@mui/material/FormControl";
import InputLabel from "@mui/material/InputLabel";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Select from "@mui/material/Select";
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
import SearchAutocomplete from "./SearchAutocomplete";

const TABS = [
  ["all", "ALL", { ignored: "false" }],
  ["classified", "CLASSIFIED", { ignored: "false", status: "classified" }],
  ["ticketed", "TICKETED", { ignored: "false", status: "awaiting_agent" }],
  ["ignored", "IGNORED", { ignored: "true" }],
];

// Date-range options for the Inbox "Range" dropdown. Value = the ?range= sent to the API
// ("" = All Time -> no server filter). "custom" reveals start/end date pickers (?since=&until=).
const RANGES = [
  ["", "All Time"],
  ["today", "Today"],
  ["yesterday", "Yesterday"],
  ["7d", "Last 7 Days"],
  ["30d", "Last 30 Days"],
  ["this_month", "This Month"],
  ["last_month", "Last Month"],
  ["custom", "Custom Date"],
];

export default function Inbox() {
  const { refreshKey, orgId, brandId } = useOutletContext();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  // A card may deep-link to a tab, e.g. /inbox?status=ignored.
  const initialTab = params.get("status") === "ignored" ? 3 : 0;
  const [tab, setTab] = useState(initialTab);
  const [search, setSearch] = useState("");
  // Date-range filter -- persisted in localStorage so it survives navigating away and back.
  const [range, setRange] = useState(() => localStorage.getItem("inboxRange") || "");
  const [since, setSince] = useState(() => localStorage.getItem("inboxSince") || "");
  const [until, setUntil] = useState(() => localStorage.getItem("inboxUntil") || "");
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

  async function load(searchArg) {
    if (!brandId) return;
    setLoading(true);
    try {
      // Server-side date-range params (empty for All Time). Custom uses since/until.
      const rangeParams = range === "custom"
        ? { ...(since ? { since } : {}), ...(until ? { until } : {}) }
        : (range ? { range } : {});
      const s = searchArg !== undefined ? searchArg : search;
      const scope = { organization: orgId, brand: brandId, search: s, ...rangeParams };
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
          (a, b) => new Date(b.last_activity_at || b.created_at)
                  - new Date(a.last_activity_at || a.created_at));
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
  }, [orgId, brandId, tab, refreshKey, range, since, until]);

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
        <SearchAutocomplete
          value={search} onChange={setSearch} onSearch={(t) => load(t)}
          placeholder="Search subject / sender…" sx={{ minWidth: 280 }}
          orgId={orgId} brandId={brandId}
        />
        <FormControl size="small" sx={{ minWidth: 150 }}>
          <InputLabel id="inbox-range-label">Range</InputLabel>
          <Select
            labelId="inbox-range-label" label="Range" value={range}
            onChange={(e) => {
              const v = e.target.value;
              setRange(v);
              localStorage.setItem("inboxRange", v);
            }}
          >
            {RANGES.map(([v, label]) => (
              <MenuItem key={v || "all"} value={v}>{label}</MenuItem>
            ))}
          </Select>
        </FormControl>
        {range === "custom" && (
          <>
            <TextField
              size="small" type="date" label="Start" InputLabelProps={{ shrink: true }}
              value={since}
              onChange={(e) => { setSince(e.target.value); localStorage.setItem("inboxSince", e.target.value); }}
            />
            <TextField
              size="small" type="date" label="End" InputLabelProps={{ shrink: true }}
              value={until}
              onChange={(e) => { setUntil(e.target.value); localStorage.setItem("inboxUntil", e.target.value); }}
            />
          </>
        )}
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
            {rows.map((t) => {
              const unread = !!t.agent_unread;
              const received = t.last_activity_at || t.created_at;
              // "You: ..." when we sent the latest message; otherwise just the customer's snippet.
              const prefix = t.last_from && t.last_from !== (t.customer_email || "")
                ? `${t.last_from}: ` : "";
              return (
              <TableRow
                key={t.id} hover
                sx={{ cursor: "pointer", ...(unread ? { bgcolor: "#f6faff" } : {}) }}
                onClick={() => navigate(
                  t._kind === "pending" ? `/pending/${t._pid}` : `/tickets/${t.id}`)}
              >
                <TableCell sx={{ fontWeight: unread ? 700 : 400 }}>
                  <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                    {unread && <Box sx={{ width: 8, height: 8, borderRadius: "50%",
                                          bgcolor: "#1a73e8", flexShrink: 0 }} />}
                    {t.customer_email || "—"}
                  </Box>
                </TableCell>
                <TableCell>
                  <Typography variant="body2" sx={{ fontWeight: unread ? 700 : 400 }}>
                    {t.subject || "—"}
                  </Typography>
                  {t.last_preview && (
                    <Typography variant="caption" color="text.secondary" noWrap
                                sx={{ display: "block", maxWidth: 460 }}>
                      {prefix}{t.last_preview}
                    </Typography>
                  )}
                </TableCell>
                <TableCell>{t.sub_topic || t.category || "—"}</TableCell>
                <TableCell><StatusChip status={t.status} label={t.status_display} /></TableCell>
                <TableCell>{t.ai_confidence != null ? t.ai_confidence.toFixed(2) : "—"}</TableCell>
                <TableCell>{fmtDate(received)}</TableCell>
                {isIgnoredTab && (
                  <TableCell align="right">
                    <Button size="small" variant="outlined"
                      onClick={(e) => unignore(t.id, e)}>
                      Un-ignore
                    </Button>
                  </TableCell>
                )}
              </TableRow>
              );
            })}
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
