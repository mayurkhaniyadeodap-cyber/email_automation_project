import { useEffect, useState } from "react";
import { useNavigate, useOutletContext, useSearchParams } from "react-router-dom";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import SearchAutocomplete from "./SearchAutocomplete";
import Typography from "@mui/material/Typography";
import { api, attachmentUrl } from "../api";
import { useSupportEmails } from "../useSupportEmails.js";
import { fmtDate } from "./chips.jsx";

const TERMINAL = ["resolved", "ignored", "ticket_created"];

// Attachment links served from /api/attachments/ need the base path (sub-path deploy) + token
// (an <a> can't send headers); attachmentUrl handles both and passes others through unchanged.
const attHref = (url) => attachmentUrl(url);

// HIGH-priority escalation INBOX (Gmail / Zendesk style): left list, right conversation, all
// actions in the top action bar of the detail view.
export default function Escalations() {
  const { refreshKey, orgId, brandId, refreshNotifications } = useOutletContext();
  const { emails: senders, defaultEmail } = useSupportEmails(orgId, brandId);
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || "";
  const [search, setSearch] = useState("");
  const [range, setRange] = useState("");
  const [rows, setRows] = useState([]);
  const [selId, setSelId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [dialog, setDialog] = useState(null);   // 'reply' | 'note' | 'assign'
  const [form, setForm] = useState({});

  async function loadList(searchArg) {
    if (!brandId) return;
    const s = searchArg !== undefined ? searchArg : search;
    const q = { organization: orgId, brand: brandId };
    if (status) q.status = status;
    if (s) q.search = s;
    if (range) q.range = range;
    const res = await api.get("/escalations/", q);
    setRows(res.results || res);
  }
  async function loadDetail(id) {
    setSelId(id);
    const d = await api.get(`/escalations/${id}/`);   // marks read
    setDetail(d);
    loadList();
    refreshNotifications?.();   // opening marks it read -> update the sidebar badge immediately
  }

  useEffect(() => { loadList(); /* eslint-disable-next-line */ }, [orgId, brandId, status, range, refreshKey]);

  // Deep-link: /escalations?open=<id> opens that escalation directly (e.g. from a report row).
  const openId = params.get("open");
  useEffect(() => {
    if (openId && brandId && String(openId) !== String(selId)) loadDetail(openId);
    /* eslint-disable-next-line */
  }, [openId, brandId]);

  async function act(action, body) {
    const d = await api.post(`/escalations/${selId}/${action}/`, body || {});
    if (action === "create-ticket" && d?.ticket_id) { navigate("/tickets"); return; }
    setDetail(d); loadList(); setDialog(null); setForm({});
    refreshNotifications?.();   // resolve / ignore / etc. change the unread badge
  }

  // Create Ticket: open a dialog with a "Notify customer" toggle (default ON).
  function createTicket() {
    setForm({ notify: true });
    setDialog("createticket");
  }

  async function sendReply() {
    const fd = new FormData();
    fd.append("body", form.body || "");
    fd.append("subject", form.subject ?? `Re: ${detail?.subject || ""}`);
    fd.append("from_email", form.from ?? defaultEmail);
    (form.files || []).forEach((f) => fd.append("attachments", f));
    try {
      const d = await api.postForm(`/escalations/${selId}/reply/`, fd);
      setDetail(d); loadList(); setDialog(null); setForm({});
    } catch (err) {
      // Send failed (SMTP error) -> keep the dialog open so the agent can retry.
      alert(err?.data?.detail || "The reply could not be sent. Check SMTP settings / logs.");
      if (selId) loadDetail(selId);
    }
  }

  const open = rows.filter((r) => !TERMINAL.includes(r.status));
  const statusFilters = [
    ["", "All"], ["manual_review_required", "Manual Review"], ["awaiting_customer_reply", "Awaiting Reply"],
    ["pending", "Pending"], ["resolved", "Resolved"], ["ignored", "Ignored"],
  ];
  const ranges = [
    ["", "All time"], ["today", "Today"], ["yesterday", "Yesterday"],
    ["7d", "Last 7 Days"], ["30d", "Last 30 Days"],
  ];

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 1, color: "#c62828" }}>
        High Priority / Escalation ({open.length})
      </Typography>
      <Stack direction="row" spacing={1} sx={{ mb: 2, alignItems: "center", flexWrap: "wrap" }}>
        <TextField select size="small" label="Status" value={status} sx={{ minWidth: 170 }}
          onChange={(e) => setParams(e.target.value ? { status: e.target.value } : {})}>
          {statusFilters.map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
        </TextField>
        <TextField select size="small" label="Range" value={range} sx={{ minWidth: 150 }}
          onChange={(e) => setRange(e.target.value)}>
          {ranges.map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
        </TextField>
        <SearchAutocomplete
          value={search} onChange={setSearch} onSearch={(t) => loadList(t)}
          placeholder="Search sender / subject / body…" sx={{ minWidth: 280 }}
          orgId={orgId} brandId={brandId} />
        <Button variant="outlined" onClick={() => loadList()}>Search</Button>
      </Stack>

      <Box sx={{ display: "flex", gap: 2, height: "calc(100vh - 220px)" }}>
        {/* LEFT: list */}
        <Paper variant="outlined" sx={{ width: 360, overflowY: "auto", flexShrink: 0 }}>
          {rows.length === 0 && <Box sx={{ p: 3, color: "text.secondary" }}>No escalations.</Box>}
          {rows.map((r) => (
            <Box key={r.id} onClick={() => loadDetail(r.id)}
              sx={{ p: 1.5, cursor: "pointer", borderBottom: "1px solid #eee",
                bgcolor: r.id === selId ? "#fff3f3" : r.is_read ? "transparent" : "#fafafa",
                borderLeft: r.id === selId ? "3px solid #c62828" : "3px solid transparent" }}>
              <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                {!r.is_read && <Box sx={{ width: 8, height: 8, borderRadius: "50%", bgcolor: "#c62828" }} />}
                <Chip size="small" label="HIGH" color="error" sx={{ height: 18, fontSize: 11 }} />
                <Typography variant="caption" sx={{ ml: "auto", color: "text.secondary" }}>
                  {fmtDate(r.received_at || r.created_at)}</Typography>
              </Box>
              <Typography sx={{ fontWeight: r.is_read ? 400 : 700, mt: 0.5 }} noWrap>
                {r.sender_name || r.sender}</Typography>
              <Typography variant="body2" color="text.secondary" noWrap>{r.subject || "(no subject)"}</Typography>
              <Box sx={{ display: "flex", gap: 1, mt: 0.5, alignItems: "center" }}>
                <Chip size="small" variant="outlined" label={r.matched_keyword} sx={{ height: 18, fontSize: 10 }} />
                <Typography variant="caption" color="text.secondary">{r.status_display || r.status}</Typography>
              </Box>
            </Box>
          ))}
        </Paper>

        {/* RIGHT: conversation */}
        <Paper variant="outlined" sx={{ flexGrow: 1, overflowY: "auto", p: 0 }}>
          {!detail ? (
            <Box sx={{ p: 5, color: "text.secondary", textAlign: "center" }}>
              Select an escalation to view the full conversation.</Box>
          ) : <Detail d={detail} onAct={act} onCreateTicket={createTicket}
                openDialog={(t) => { setDialog(t); setForm({}); }} />}
        </Paper>
      </Box>

      {/* dialogs */}
      <Dialog open={dialog === "reply"} onClose={() => setDialog(null)} fullWidth maxWidth="sm">
        <DialogTitle>Reply to {detail?.sender}</DialogTitle>
        <DialogContent>
          <TextField label="To" fullWidth size="small" sx={{ mt: 1 }}
            value={detail?.sender || ""} InputProps={{ readOnly: true }} />
          {senders.length > 0 && (
            <TextField select label="Reply From" fullWidth size="small" sx={{ mt: 2 }}
              value={form.from ?? defaultEmail}
              onChange={(e) => setForm({ ...form, from: e.target.value })}>
              {senders.map((s) => (
                <MenuItem key={s.id} value={s.email}>
                  {s.email}{s.is_primary ? " (primary)" : ""}</MenuItem>
              ))}
            </TextField>
          )}
          <TextField label="Subject" fullWidth size="small" sx={{ mt: 2 }}
            value={form.subject ?? `Re: ${detail?.subject || ""}`}
            onChange={(e) => setForm({ ...form, subject: e.target.value })} />
          <TextField label="Message" multiline minRows={6} fullWidth sx={{ mt: 2 }}
            placeholder="Type your reply…" value={form.body || ""}
            onChange={(e) => setForm({ ...form, body: e.target.value })} />
          <Box sx={{ mt: 2, display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
            <Button variant="outlined" component="label" size="small">
              Attach Files
              <input type="file" multiple hidden
                onChange={(e) => setForm({ ...form, files: [...(form.files || []), ...e.target.files] })} />
            </Button>
            {(form.files || []).map((f, i) => (
              <Chip key={i} size="small" label={f.name}
                onDelete={() => setForm({ ...form, files: form.files.filter((_, j) => j !== i) })} />
            ))}
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => act("draft", { draft: form.body || "" })}>Save Draft</Button>
          <Button onClick={() => setDialog(null)}>Cancel</Button>
          <Button variant="contained" disabled={!form.body?.trim()} onClick={sendReply}>Send</Button>
        </DialogActions>
      </Dialog>

      <Dialog open={dialog === "createticket"} onClose={() => setDialog(null)} fullWidth maxWidth="sm">
        <DialogTitle>Create ticket from escalation</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mb: 1 }}>
            A HIGH-priority ticket will be created and the customer's photos/videos attached to it.
          </Typography>
          <FormControlLabel
            control={<Checkbox checked={form.notify ?? true}
              onChange={(e) => setForm({ ...form, notify: e.target.checked })} />}
            label="Notify customer & push to Care Panel" />
          <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
            {form.notify ?? true
              ? "Emails the customer a 'ticket created' confirmation and pushes to the Care Panel (with media). A tracking link needs an order id / phone."
              : "Internal only — no customer email (use for pure legal cases)."}
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialog(null)}>Cancel</Button>
          <Button variant="contained" color="error"
            onClick={() => act("create-ticket", { notify: form.notify ?? true })}>
            Create Ticket</Button>
        </DialogActions>
      </Dialog>

      <Dialog open={dialog === "note"} onClose={() => setDialog(null)} fullWidth maxWidth="sm">
        <DialogTitle>Internal note (not emailed)</DialogTitle>
        <DialogContent>
          <TextField autoFocus multiline minRows={4} fullWidth sx={{ mt: 1 }}
            placeholder="Visible only in the Care Panel…" value={form.note || ""}
            onChange={(e) => setForm({ ...form, note: e.target.value })} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialog(null)}>Cancel</Button>
          <Button variant="contained" disabled={!form.note?.trim()}
            onClick={() => act("note", { note: form.note })}>Add Note</Button>
        </DialogActions>
      </Dialog>

      <Dialog open={dialog === "assign"} onClose={() => setDialog(null)} fullWidth maxWidth="xs">
        <DialogTitle>Assign agent</DialogTitle>
        <DialogContent>
          <TextField autoFocus fullWidth size="small" sx={{ mt: 1 }} placeholder="agent email / name"
            value={form.agent || ""} onChange={(e) => setForm({ ...form, agent: e.target.value })} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialog(null)}>Cancel</Button>
          <Button variant="contained" disabled={!form.agent?.trim()}
            onClick={() => act("assign", { assigned_to: form.agent })}>Assign</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

function Field({ label, value }) {
  return value ? (
    <Typography variant="body2"><b>{label}:</b> {value}</Typography>
  ) : null;
}

function Detail({ d, onAct, onCreateTicket, openDialog }) {
  const readOnly = TERMINAL.includes(d.status);
  return (
    <Box>
      {/* ACTION BAR */}
      <Box sx={{ p: 1.5, position: "sticky", top: 0, bgcolor: "#fff", zIndex: 1,
        borderBottom: "1px solid #eee", display: "flex", gap: 1, flexWrap: "wrap" }}>
        {!readOnly && <>
          <Button size="small" variant="contained" onClick={() => openDialog("reply")}>Reply</Button>
          <Button size="small" variant="contained" color="error"
            onClick={onCreateTicket}>Create Ticket</Button>
          <Button size="small" onClick={() => onAct("resolve")}>Resolve</Button>
          <Button size="small" color="inherit" onClick={() => onAct("ignore")}>Ignore</Button>
          <Button size="small" onClick={() => onAct("pending")}>Mark Pending</Button>
          <Button size="small" onClick={() => openDialog("assign")}>Assign Agent</Button>
          <Button size="small" onClick={() => openDialog("note")}>Add Internal Note</Button>
        </>}
        {readOnly && <Chip label={`${d.status_display} — read only`} size="small" />}
      </Box>

      {/* HEADER */}
      <Box sx={{ p: 2 }}>
        <Typography variant="h6">{d.subject || "(no subject)"}</Typography>
        <Stack direction="row" spacing={1} sx={{ my: 1, flexWrap: "wrap" }}>
          <Chip size="small" label="HIGH" color="error" />
          <Chip size="small" variant="outlined" label={d.matched_keyword} />
          <Chip size="small" label={d.status_display || d.status} />
        </Stack>
        <Field label="Sender" value={d.sender_name || "—"} />
        <Field label="Email" value={d.sender} />
        <Field label="Received" value={fmtDate(d.received_at || d.created_at)} />
        <Field label="Assigned To" value={d.assigned_to} />
        <Field label="Assigned Time" value={d.assigned_at && fmtDate(d.assigned_at)} />
        {(d.attachments || []).length > 0 && (
          <Box sx={{ mt: 1 }}>
            <b style={{ fontSize: 13 }}>Attachments:</b>{" "}
            {d.attachments.map((a, i) => (
              <a key={i} href={attHref(a.url)} target="_blank" rel="noreferrer" style={{ marginRight: 12 }}>
                {a.filename}</a>
            ))}
          </Box>
        )}
      </Box>
      <Divider />

      {/* CONVERSATION (newest at bottom) */}
      <Box sx={{ p: 2, display: "flex", flexDirection: "column", gap: 1.5 }}>
        {(d.conversation || []).map((m, i) => <Message key={i} m={m} />)}
      </Box>

      {/* TIMELINE */}
      {(d.timeline || []).length > 0 && (
        <>
          <Divider />
          <Box sx={{ p: 2 }}>
            <Typography variant="subtitle2" sx={{ mb: 1 }}>Activity timeline</Typography>
            {d.timeline.map((e, i) => (
              <Typography key={i} variant="caption" display="block" color="text.secondary">
                {fmtDate(e.at)} — {e.event.replace(/_/g, " ")}
                {e.detail?.assigned_to ? ` → ${e.detail.assigned_to}` : ""}
                {e.detail?.ticket_id ? ` (${e.detail.ticket_id})` : ""}
                {e.actor ? ` · ${e.actor}` : ""}
              </Typography>
            ))}
          </Box>
        </>
      )}
    </Box>
  );
}

function Message({ m }) {
  const isNote = m.direction === "note";
  const isOut = m.direction === "outbound";
  const bg = isNote ? "#fff8e1" : isOut ? "#e3f2fd" : "#f5f5f5";
  const who = isNote ? `Internal note · ${m.agent || ""}`
    : isOut ? `Sent from ${m.from || m.agent || "agent"}${m.from && m.agent ? ` · by ${m.agent}` : ""}`
    : `Customer · ${m.from || ""}`;
  return (
    <Box sx={{ bgcolor: bg, borderRadius: 1, p: 1.5, border: "1px solid #eee" }}>
      <Typography variant="caption" sx={{ fontWeight: 700 }}>{who}</Typography>
      <Typography variant="caption" sx={{ float: "right", color: "text.secondary" }}>
        {m.at && fmtDate(m.at)}</Typography>
      {m.body_html
        ? <Box sx={{ mt: 0.5, "& img": { maxWidth: "100%" } }}
            dangerouslySetInnerHTML={{ __html: m.body_html }} />
        : <Typography variant="body2" sx={{ mt: 0.5, whiteSpace: "pre-wrap" }}>{m.body}</Typography>}
      {(m.attachments || []).map((a, i) => (
        <a key={i} href={attHref(a.url)} target="_blank" rel="noreferrer"
          style={{ display: "block", fontSize: 13, marginTop: 4 }}>{a.filename}</a>
      ))}
    </Box>
  );
}
