import { useEffect, useState } from "react";
import { useOutletContext, useSearchParams } from "react-router-dom";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Divider from "@mui/material/Divider";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import SearchAutocomplete from "./SearchAutocomplete";
import Typography from "@mui/material/Typography";
import { api, attachmentUrl } from "../api";
import { useSupportEmails } from "../useSupportEmails.js";
import { fmtDate } from "./chips.jsx";

// Attachment links (served from /api/attachments/) need the base path (sub-path deploy) + token;
// attachmentUrl handles both and passes non-attachment URLs through unchanged.
const attHref = (url) => attachmentUrl(url);

// Internal Communications INBOX (Gmail-style). Completely independent of tickets / escalations:
// these are internal company emails, never customer support. Left list, right conversation.
export default function InternalComms() {
  const { refreshKey, orgId, brandId, refreshNotifications } = useOutletContext();
  const { emails: senders, defaultEmail } = useSupportEmails(orgId, brandId);
  const [params, setParams] = useSearchParams();
  const status = params.get("status") || "";
  const [search, setSearch] = useState("");
  const [rows, setRows] = useState([]);
  const [selId, setSelId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [dialog, setDialog] = useState(null);   // 'reply' | 'forward' | 'note' | 'assign'
  const [form, setForm] = useState({});

  async function loadList(searchArg) {
    if (!brandId) return;
    const s = searchArg !== undefined ? searchArg : search;
    const q = { organization: orgId, brand: brandId };
    if (status) q.status = status;
    if (s) q.search = s;
    const res = await api.get("/internal-emails/", q);
    setRows(res.results || res);
  }
  async function loadDetail(id) {
    setSelId(id);
    setDetail(await api.get(`/internal-emails/${id}/`));   // marks read
    loadList();
    refreshNotifications?.();   // opening marks it read -> update the sidebar badge immediately
  }

  useEffect(() => { loadList(); /* eslint-disable-next-line */ }, [orgId, brandId, status, refreshKey]);

  const openId = params.get("open");
  useEffect(() => {
    if (openId && brandId && String(openId) !== String(selId)) loadDetail(openId);
    /* eslint-disable-next-line */
  }, [openId, brandId]);

  async function act(action, body) {
    const d = await api.post(`/internal-emails/${selId}/${action}/`, body || {});
    setDetail(d); loadList(); setDialog(null); setForm({});
    refreshNotifications?.();   // mark-read / mark-unread / archive change the unread badge
  }

  async function sendReply(kind) {
    const fd = new FormData();
    fd.append("body", form.body || "");
    fd.append("from_email", form.from ?? defaultEmail);
    if (kind === "forward") fd.append("to", form.to || "");
    else fd.append("subject", form.subject ?? `Re: ${detail?.subject || ""}`);
    (form.files || []).forEach((f) => fd.append("attachments", f));
    try {
      const d = await api.postForm(`/internal-emails/${selId}/${kind}/`, fd);
      setDetail(d); loadList(); setDialog(null); setForm({});
    } catch (err) {
      alert(err?.data?.detail || "The email could not be sent. Check SMTP settings / logs.");
      if (selId) loadDetail(selId);
    }
  }

  const statusFilters = [
    ["", "All"], ["internal_review", "Pending"], ["awaiting_reply", "Replied"],
    ["archived", "Archived"],
  ];

  return (
    <Box>
      <Typography variant="h5" sx={{ mb: 1, color: "#5e35b1" }}>
        Internal Communications ({rows.length})
      </Typography>
      <Typography variant="body2" sx={{ mb: 2, color: "text.secondary" }}>
        Emails sent to internal company addresses. Handled separately — never create tickets or
        customer automation.
      </Typography>
      <Stack direction="row" spacing={1} sx={{ mb: 2, alignItems: "center", flexWrap: "wrap" }}>
        <TextField select size="small" label="Status" value={status} sx={{ minWidth: 170 }}
          onChange={(e) => setParams(e.target.value ? { status: e.target.value } : {})}>
          {statusFilters.map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
        </TextField>
        <SearchAutocomplete
          value={search} onChange={setSearch} onSearch={(t) => loadList(t)}
          placeholder="Search sender / subject / body…" sx={{ minWidth: 280 }}
          orgId={orgId} brandId={brandId} />
        <Button variant="outlined" onClick={() => loadList()}>Search</Button>
      </Stack>

      <Box sx={{ display: "flex", gap: 2, height: "calc(100vh - 240px)" }}>
        {/* LEFT: list */}
        <Paper variant="outlined" sx={{ width: 360, overflowY: "auto", flexShrink: 0 }}>
          {rows.length === 0 && <Box sx={{ p: 3, color: "text.secondary" }}>No internal emails.</Box>}
          {rows.map((r) => (
            <Box key={r.id} onClick={() => loadDetail(r.id)}
              sx={{ p: 1.5, cursor: "pointer", borderBottom: "1px solid #eee",
                bgcolor: r.id === selId ? "#f3eefb" : r.is_read ? "transparent" : "#fafafa",
                borderLeft: r.id === selId ? "3px solid #5e35b1" : "3px solid transparent" }}>
              <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                {!r.is_read && <Box sx={{ width: 8, height: 8, borderRadius: "50%", bgcolor: "#5e35b1" }} />}
                <Chip size="small" label="INTERNAL" sx={{ height: 18, fontSize: 10, bgcolor: "#ede7f6" }} />
                <Typography variant="caption" sx={{ ml: "auto", color: "text.secondary" }}>
                  {fmtDate(r.received_at || r.created_at)}</Typography>
              </Box>
              <Typography sx={{ fontWeight: r.is_read ? 400 : 700, mt: 0.5 }} noWrap>
                {r.sender_name || r.sender}</Typography>
              <Typography variant="body2" color="text.secondary" noWrap>{r.subject || "(no subject)"}</Typography>
              <Typography variant="caption" color="text.secondary">{r.status_display || r.status}</Typography>
            </Box>
          ))}
        </Paper>

        {/* RIGHT: conversation */}
        <Paper variant="outlined" sx={{ flexGrow: 1, overflowY: "auto", p: 0 }}>
          {!detail ? (
            <Box sx={{ p: 5, color: "text.secondary", textAlign: "center" }}>
              Select an email to view the full conversation.</Box>
          ) : <Detail d={detail} onAct={act} openDialog={(t) => { setDialog(t); setForm({}); }} />}
        </Paper>
      </Box>

      {/* reply / forward dialog */}
      <Dialog open={dialog === "reply" || dialog === "forward"} onClose={() => setDialog(null)}
        fullWidth maxWidth="sm">
        <DialogTitle>{dialog === "forward" ? "Forward email" : `Reply to ${detail?.sender}`}</DialogTitle>
        <DialogContent>
          {dialog === "forward" ? (
            <TextField label="To" fullWidth size="small" sx={{ mt: 1 }} placeholder="recipient@…"
              value={form.to || ""} onChange={(e) => setForm({ ...form, to: e.target.value })} />
          ) : (
            <TextField label="To" fullWidth size="small" sx={{ mt: 1 }}
              value={detail?.sender || ""} InputProps={{ readOnly: true }} />
          )}
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
          {dialog === "reply" && (
            <TextField label="Subject" fullWidth size="small" sx={{ mt: 2 }}
              value={form.subject ?? `Re: ${detail?.subject || ""}`}
              onChange={(e) => setForm({ ...form, subject: e.target.value })} />
          )}
          <TextField label="Message" multiline minRows={6} fullWidth sx={{ mt: 2 }}
            placeholder="Type your message…" value={form.body || ""}
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
          <Button onClick={() => setDialog(null)}>Cancel</Button>
          <Button variant="contained"
            disabled={dialog === "forward" ? !form.to?.trim() : !form.body?.trim()}
            onClick={() => sendReply(dialog)}>{dialog === "forward" ? "Forward" : "Send"}</Button>
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
        <DialogTitle>Assign employee</DialogTitle>
        <DialogContent>
          <TextField autoFocus fullWidth size="small" sx={{ mt: 1 }} placeholder="employee email / name"
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
  return value ? <Typography variant="body2"><b>{label}:</b> {value}</Typography> : null;
}

function Detail({ d, onAct, openDialog }) {
  const isDeleted = d.status === "deleted";
  return (
    <Box>
      {/* ACTION BAR */}
      <Box sx={{ p: 1.5, position: "sticky", top: 0, bgcolor: "#fff", zIndex: 1,
        borderBottom: "1px solid #eee", display: "flex", gap: 1, flexWrap: "wrap" }}>
        <Button size="small" variant="contained" onClick={() => openDialog("reply")}>Reply</Button>
        <Button size="small" variant="outlined" onClick={() => openDialog("forward")}>Forward</Button>
        {d.is_read
          ? <Button size="small" onClick={() => onAct("mark-unread")}>Mark Unread</Button>
          : <Button size="small" onClick={() => onAct("mark-read")}>Mark Read</Button>}
        {d.status !== "archived" &&
          <Button size="small" onClick={() => onAct("archive")}>Archive</Button>}
        {!isDeleted &&
          <Button size="small" color="error" onClick={() => onAct("delete")}>Delete</Button>}
        <Button size="small" onClick={() => openDialog("assign")}>Assign Employee</Button>
        <Button size="small" onClick={() => openDialog("note")}>Add Internal Note</Button>
      </Box>

      {/* HEADER */}
      <Box sx={{ p: 2 }}>
        <Typography variant="h6">{d.subject || "(no subject)"}</Typography>
        <Stack direction="row" spacing={1} sx={{ my: 1, flexWrap: "wrap" }}>
          <Chip size="small" label="INTERNAL" sx={{ bgcolor: "#ede7f6" }} />
          <Chip size="small" label={d.status_display || d.status} />
        </Stack>
        <Field label="From" value={d.sender_name ? `${d.sender_name} <${d.sender}>` : d.sender} />
        <Field label="To" value={(d.to_addrs || []).join(", ")} />
        <Field label="Internal recipient" value={d.matched_recipient} />
        <Field label="Received" value={fmtDate(d.received_at || d.created_at)} />
        <Field label="Assigned To" value={d.assigned_to} />
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

      {/* CONVERSATION */}
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
  const bg = isNote ? "#fff8e1" : isOut ? "#ede7f6" : "#f5f5f5";
  const who = isNote ? `Internal note · ${m.agent || ""}`
    : isOut ? `${m.forward ? "Forwarded" : "Reply"} from ${m.from || m.agent || "agent"}`
      + `${m.to ? ` → ${m.to}` : ""}`
    : `From · ${m.from || ""}`;
  return (
    <Box sx={{ bgcolor: bg, borderRadius: 1, p: 1.5, border: "1px solid #eee" }}>
      <Typography variant="caption" sx={{ fontWeight: 700 }}>{who}</Typography>
      <Typography variant="caption" sx={{ float: "right", color: "text.secondary" }}>
        {m.at && fmtDate(m.at)}{m.failed ? " · SEND FAILED" : ""}</Typography>
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
