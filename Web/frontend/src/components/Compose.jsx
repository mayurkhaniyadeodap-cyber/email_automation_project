import { useEffect, useRef, useState } from "react";
import { useOutletContext } from "react-router-dom";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Snackbar from "@mui/material/Snackbar";
import Alert from "@mui/material/Alert";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import FormatBoldIcon from "@mui/icons-material/FormatBold";
import FormatItalicIcon from "@mui/icons-material/FormatItalic";
import FormatUnderlinedIcon from "@mui/icons-material/FormatUnderlined";
import FormatListBulletedIcon from "@mui/icons-material/FormatListBulleted";
import FormatListNumberedIcon from "@mui/icons-material/FormatListNumbered";
import LinkIcon from "@mui/icons-material/Link";
import AttachFileIcon from "@mui/icons-material/AttachFile";
import SendIcon from "@mui/icons-material/Send";
import SaveOutlinedIcon from "@mui/icons-material/SaveOutlined";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutlined";
import EditIcon from "@mui/icons-material/EditOutlined";
import ReplyIcon from "@mui/icons-material/Reply";
import AttachmentIcon from "@mui/icons-material/AttachmentOutlined";
import { api, attachmentUrl } from "../api";
import { useSupportEmails } from "../useSupportEmails.js";

const fmt = (s) => (s ? new Date(s).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "");
const STATUS_COLOR = { draft: "default", sent: "success", failed: "error" };
const lastParticipant = (r) => {
  const convo = r.conversation || [];
  const last = convo[convo.length - 1];
  if (last) return last.direction === "inbound" ? (last.from || "") : (last.to || r.to_addrs);
  return r.to_addrs;
};

// Self-contained rich text editor (contentEditable + execCommand -> HTML). No dependency.
function RichEditor({ editorRef, minHeight = 200, placeholder = "Write your message…" }) {
  const exec = (cmd, val = null) => { editorRef.current?.focus(); document.execCommand(cmd, false, val); };
  const addLink = () => { const u = window.prompt("Link URL:", "https://"); if (u) exec("createLink", u); };
  const btn = (title, icon, on) => (
    <Tooltip title={title}>
      <IconButton size="small" onMouseDown={(e) => e.preventDefault()} onClick={on}>{icon}</IconButton>
    </Tooltip>
  );
  return (
    <>
      <Box sx={{ display: "flex", alignItems: "center", flexWrap: "wrap",
                 border: "1px solid #e0e0e0", borderBottom: "none", borderRadius: "4px 4px 0 0",
                 bgcolor: "#fafafa", px: 0.5 }}>
        {btn("Bold", <FormatBoldIcon fontSize="small" />, () => exec("bold"))}
        {btn("Italic", <FormatItalicIcon fontSize="small" />, () => exec("italic"))}
        {btn("Underline", <FormatUnderlinedIcon fontSize="small" />, () => exec("underline"))}
        <Divider orientation="vertical" flexItem sx={{ mx: 0.5, my: 1 }} />
        {btn("Bulleted list", <FormatListBulletedIcon fontSize="small" />, () => exec("insertUnorderedList"))}
        {btn("Numbered list", <FormatListNumberedIcon fontSize="small" />, () => exec("insertOrderedList"))}
        {btn("Insert link", <LinkIcon fontSize="small" />, addLink)}
      </Box>
      <Box ref={editorRef} contentEditable suppressContentEditableWarning
           sx={{ minHeight, p: 1.5, border: "1px solid #e0e0e0", borderRadius: "0 0 4px 4px",
                 outline: "none", fontSize: 14, lineHeight: 1.6, overflowY: "auto",
                 "&:empty::before": { content: `"${placeholder}"`, color: "#9aa" },
                 "& a": { color: "#1a73e8" } }} />
    </>
  );
}

function AttachmentChips({ items }) {
  if (!items || items.length === 0) return null;
  return (
    <Box sx={{ mt: 1, display: "flex", gap: 1, flexWrap: "wrap" }}>
      {items.map((a, i) => (
        <Chip key={i} size="small" icon={<AttachmentIcon />} label={a.filename}
              component="a" clickable target="_blank" rel="noreferrer"
              href={attachmentUrl(a.url)} />
      ))}
    </Box>
  );
}

export default function Compose() {
  const { orgId, brandId } = useOutletContext();
  const { emails: senders, defaultEmail } = useSupportEmails(orgId, brandId);
  const editorRef = useRef(null);
  const replyRef = useRef(null);

  const [view, setView] = useState("compose");   // "compose" | "thread"
  const [selected, setSelected] = useState(null); // ComposedEmail (with conversation) in thread view

  // composer (new / draft)
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [cc, setCc] = useState("");
  const [bcc, setBcc] = useState("");
  const [showCc, setShowCc] = useState(false);
  const [showBcc, setShowBcc] = useState(false);
  const [subject, setSubject] = useState("");
  const [files, setFiles] = useState([]);
  const [editingId, setEditingId] = useState(null);

  // reply (inside a thread)
  const [replyFiles, setReplyFiles] = useState([]);

  const [busy, setBusy] = useState("");
  const [snack, setSnack] = useState(null);
  const [recent, setRecent] = useState([]);

  useEffect(() => { if (!from && defaultEmail) setFrom(defaultEmail); }, [defaultEmail]); // eslint-disable-line

  async function loadRecent() {
    if (!brandId) return;
    try {
      const d = await api.get("/compose-emails/",
        { organization: orgId, brand: brandId, ordering: "-updated_at" });
      setRecent((d.results || d).slice(0, 40));
    } catch { /* non-fatal */ }
  }
  useEffect(() => { loadRecent(); }, [orgId, brandId]); // eslint-disable-line

  function newCompose() {
    setView("compose"); setSelected(null);
    setEditingId(null); setTo(""); setCc(""); setBcc(""); setSubject("");
    setShowCc(false); setShowBcc(false); setFiles([]); setFrom(defaultEmail || "");
    if (editorRef.current) editorRef.current.innerHTML = "";
  }

  function loadDraftIntoComposer(rec) {
    setView("compose"); setSelected(null);
    setEditingId(rec.id);
    setFrom(rec.from_email || defaultEmail || "");
    setTo(rec.to_addrs || ""); setCc(rec.cc || ""); setBcc(rec.bcc || "");
    setShowCc(!!rec.cc); setShowBcc(!!rec.bcc);
    setSubject(rec.subject || ""); setFiles([]);
    if (editorRef.current) editorRef.current.innerHTML = rec.body_html || "";
  }

  async function openThread(rec) {
    try {
      const full = await api.get(`/compose-emails/${rec.id}/`);
      setSelected(full); setView("thread"); setReplyFiles([]);
      if (replyRef.current) replyRef.current.innerHTML = "";
      if (!full.is_read) { api.post(`/compose-emails/${rec.id}/mark-read/`).then(loadRecent).catch(() => {}); }
    } catch {
      setSnack({ severity: "error", msg: "Could not open the conversation." });
    }
  }

  function clickRecord(rec) {
    if (rec.status === "draft" && !(rec.conversation || []).length) loadDraftIntoComposer(rec);
    else openThread(rec);
  }

  function buildForm() {
    const fd = new FormData();
    fd.append("brand", brandId);
    fd.append("from_email", from || defaultEmail || "");
    fd.append("to", to); fd.append("cc", cc); fd.append("bcc", bcc);
    fd.append("subject", subject);
    fd.append("body_html", editorRef.current?.innerHTML || "");
    fd.append("body_text", editorRef.current?.innerText || "");
    if (editingId) fd.append("id", editingId);
    files.forEach((f) => fd.append("attachments", f));
    return fd;
  }

  async function saveDraft() {
    if (!brandId) return;
    setBusy("draft");
    try {
      const d = await api.postForm("/compose-emails/draft/", buildForm());
      setEditingId(d.id); setFiles([]);
      setSnack({ severity: "success", msg: "Draft saved." });
      loadRecent();
    } catch (err) {
      setSnack({ severity: "error", msg: err?.data?.detail || "Could not save draft." });
    } finally { setBusy(""); }
  }

  async function send() {
    if (!to.trim()) { setSnack({ severity: "warning", msg: "Add at least one recipient (To)." }); return; }
    setBusy("send");
    try {
      const d = await api.postForm("/compose-emails/send/", buildForm());
      setSnack({ severity: "success", msg: "Email sent." });
      setFiles([]);
      loadRecent();
      openThread(d);   // show it as a conversation right away
    } catch (err) {
      setSnack({ severity: "error",
        msg: err?.data?.detail || "The email could not be sent (SMTP). Saved as Failed." });
      loadRecent();
    } finally { setBusy(""); }
  }

  async function sendReply() {
    if (!selected) return;
    const html = replyRef.current?.innerHTML || "";
    const text = replyRef.current?.innerText || "";
    if (!text.trim() && replyFiles.length === 0) {
      setSnack({ severity: "warning", msg: "Write a reply first." }); return;
    }
    setBusy("reply");
    try {
      const fd = new FormData();
      fd.append("brand", brandId);
      fd.append("body_html", html); fd.append("body_text", text);
      replyFiles.forEach((f) => fd.append("attachments", f));
      const d = await api.postForm(`/compose-emails/${selected.id}/reply/`, fd);
      setSelected(d); setReplyFiles([]);
      if (replyRef.current) replyRef.current.innerHTML = "";
      setSnack({ severity: "success", msg: "Reply sent." });
      loadRecent();
    } catch (err) {
      setSnack({ severity: "error", msg: err?.data?.detail || "The reply could not be sent (SMTP)." });
    } finally { setBusy(""); }
  }

  async function removeRecord(e, rec) {
    e.stopPropagation();
    if (!window.confirm("Delete this draft?")) return;
    try {
      await api.del(`/compose-emails/${rec.id}/`);
      if (selected?.id === rec.id) newCompose();
      loadRecent();
    } catch { setSnack({ severity: "error", msg: "Could not delete." }); }
  }

  return (
    <Box>
      <Box sx={{ display: "flex", alignItems: "center", mb: 2, gap: 2 }}>
        <Typography variant="h5">Compose</Typography>
        <Button variant="contained" startIcon={<EditIcon />} onClick={newCompose}>New Email</Button>
      </Box>

      <Box sx={{ display: "flex", gap: 2, alignItems: "flex-start", flexWrap: "wrap" }}>
        {/* LEFT: conversation list (Drafts & Sent) */}
        <Paper variant="outlined" sx={{ flex: "0 0 320px", minWidth: 260, maxHeight: "76vh", overflowY: "auto" }}>
          <Typography variant="subtitle2" sx={{ p: 1.5, position: "sticky", top: 0, bgcolor: "#fff",
                     borderBottom: "1px solid #eee", zIndex: 1 }}>
            Drafts &amp; Sent
          </Typography>
          {recent.length === 0 && <Box sx={{ p: 3, color: "text.secondary", fontSize: 14 }}>Nothing yet.</Box>}
          {recent.map((r) => {
            const count = (r.conversation || []).length;
            return (
              <Box key={r.id} onClick={() => clickRecord(r)}
                   sx={{ p: 1.25, cursor: "pointer", borderBottom: "1px solid #f0f0f0",
                         display: "flex", flexDirection: "column", gap: 0.25,
                         bgcolor: selected?.id === r.id ? "#eef4ff" : (r.is_read ? "transparent" : "#f6faff"),
                         borderLeft: selected?.id === r.id ? "3px solid #1a73e8" : "3px solid transparent",
                         "&:hover .del": { visibility: "visible" } }}>
                <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                  {!r.is_read && <Box sx={{ width: 8, height: 8, borderRadius: "50%", bgcolor: "#1a73e8" }} />}
                  <Chip size="small" label={r.status_display || r.status}
                        color={STATUS_COLOR[r.status] || "default"} sx={{ height: 18, fontSize: 10 }} />
                  {count > 1 && <Chip size="small" label={count} variant="outlined" sx={{ height: 18, fontSize: 10 }} />}
                  <Typography variant="caption" sx={{ ml: "auto", color: "text.secondary" }}>
                    {fmt(r.updated_at || r.created_at)}
                  </Typography>
                  {r.status === "draft" && count === 0 && (
                    <IconButton className="del" size="small" onClick={(e) => removeRecord(e, r)}
                                sx={{ visibility: "hidden", p: 0.25 }}>
                      <DeleteOutlineIcon sx={{ fontSize: 16 }} />
                    </IconButton>
                  )}
                </Box>
                <Typography variant="body2" noWrap sx={{ fontWeight: r.is_read ? 500 : 700 }}>
                  {lastParticipant(r) || "(no recipient)"}
                </Typography>
                <Typography variant="caption" color="text.secondary" noWrap>
                  {r.subject || "(no subject)"}
                </Typography>
              </Box>
            );
          })}
        </Paper>

        {/* RIGHT: composer OR conversation view */}
        <Paper variant="outlined" sx={{ flex: "1 1 560px", minWidth: 320, p: 2 }}>
          {view === "thread" && selected ? (
            <ThreadView
              rec={selected} replyRef={replyRef}
              replyFiles={replyFiles} setReplyFiles={setReplyFiles}
              onSendReply={sendReply} busy={busy} />
          ) : (
            <>
              {senders.length > 0 ? (
                <TextField select fullWidth size="small" label="From" value={from || defaultEmail || ""}
                           onChange={(e) => setFrom(e.target.value)} sx={{ mb: 1 }}>
                  {senders.map((s) => (
                    <MenuItem key={s.id} value={s.email}>
                      {s.email}{s.is_primary ? " (primary)" : ""}</MenuItem>
                  ))}
                </TextField>
              ) : (
                <TextField fullWidth size="small" label="From" value={from}
                           onChange={(e) => setFrom(e.target.value)} sx={{ mb: 1 }} />
              )}
              <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                <TextField fullWidth size="small" label="To" placeholder="recipient@example.com, another@…"
                           value={to} onChange={(e) => setTo(e.target.value)} sx={{ mb: 1 }} />
                {!showCc && <Button size="small" onClick={() => setShowCc(true)}>Cc</Button>}
                {!showBcc && <Button size="small" onClick={() => setShowBcc(true)}>Bcc</Button>}
              </Box>
              {showCc && <TextField fullWidth size="small" label="Cc" value={cc}
                                    onChange={(e) => setCc(e.target.value)} sx={{ mb: 1 }} />}
              {showBcc && <TextField fullWidth size="small" label="Bcc" value={bcc}
                                     onChange={(e) => setBcc(e.target.value)} sx={{ mb: 1 }} />}
              <TextField fullWidth size="small" label="Subject" value={subject}
                         onChange={(e) => setSubject(e.target.value)} sx={{ mb: 1 }} />

              <RichEditor editorRef={editorRef} />

              <Box sx={{ mt: 1.5, display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
                <Button variant="outlined" size="small" component="label" startIcon={<AttachFileIcon />}>
                  Attach Files
                  <input type="file" multiple hidden
                         onChange={(e) => setFiles([...files, ...Array.from(e.target.files)])} />
                </Button>
                {files.map((f, i) => (
                  <Chip key={i} size="small" label={f.name}
                        onDelete={() => setFiles(files.filter((_, j) => j !== i))} />
                ))}
              </Box>

              <Box sx={{ mt: 2, display: "flex", gap: 1, alignItems: "center" }}>
                <Button variant="contained" startIcon={<SendIcon />} onClick={send}
                        disabled={busy !== "" || !brandId}>
                  {busy === "send" ? "Sending…" : "Send"}
                </Button>
                <Button variant="outlined" startIcon={<SaveOutlinedIcon />} onClick={saveDraft}
                        disabled={busy !== "" || !brandId}>
                  {busy === "draft" ? "Saving…" : "Save Draft"}
                </Button>
                {editingId && <Typography variant="caption" sx={{ color: "text.secondary" }}>
                  Editing draft #{editingId}</Typography>}
              </Box>
            </>
          )}
        </Paper>
      </Box>

      <Snackbar open={!!snack} autoHideDuration={5000} onClose={() => setSnack(null)}
                anchorOrigin={{ vertical: "bottom", horizontal: "center" }}>
        {snack ? (
          <Alert severity={snack.severity} variant="filled" onClose={() => setSnack(null)}>{snack.msg}</Alert>
        ) : <span />}
      </Snackbar>
    </Box>
  );
}

// Gmail-style conversation view: ordered bubbles (You / Customer) + a reply box.
function ThreadView({ rec, replyRef, replyFiles, setReplyFiles, onSendReply, busy }) {
  const convo = rec.conversation || [];
  return (
    <Box>
      <Typography variant="h6" sx={{ mb: 0.5 }}>{rec.subject || "(no subject)"}</Typography>
      <Typography variant="caption" color="text.secondary">
        {rec.from_email} → {rec.to_addrs}
      </Typography>
      <Divider sx={{ my: 1.5 }} />

      {convo.length === 0 && (
        <Typography variant="body2" color="text.secondary">No messages in this thread yet.</Typography>
      )}
      {convo.map((m, i) => {
        const out = m.direction === "outbound";
        return (
          <Box key={i} sx={{ display: "flex", justifyContent: out ? "flex-end" : "flex-start", mb: 1.5 }}>
            <Box sx={{ maxWidth: "82%", p: 1.5, borderRadius: 2,
                       bgcolor: out ? "#e8f0fe" : "#f5f5f5",
                       border: "1px solid", borderColor: out ? "#d2e3fc" : "#eee" }}>
              <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 0.5 }}>
                <Typography variant="caption" sx={{ fontWeight: 700 }}>
                  {out ? "You" : (m.from || "Customer")}
                </Typography>
                <Typography variant="caption" sx={{ ml: "auto", color: "text.secondary" }}>
                  {fmt(m.at)}
                </Typography>
              </Box>
              {m.body_html
                ? <Box sx={{ fontSize: 14, lineHeight: 1.6, "& img": { maxWidth: "100%" } }}
                       dangerouslySetInnerHTML={{ __html: m.body_html }} />
                : <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>{m.body_text}</Typography>}
              <AttachmentChips items={m.attachments} />
            </Box>
          </Box>
        );
      })}

      {/* Reply box -- continues the SAME thread (In-Reply-To / References) */}
      <Divider sx={{ my: 2 }} />
      <Typography variant="subtitle2" sx={{ mb: 1, display: "flex", alignItems: "center", gap: 0.5 }}>
        <ReplyIcon fontSize="small" /> Reply
      </Typography>
      <RichEditor editorRef={replyRef} minHeight={130} placeholder="Type your reply…" />
      <Box sx={{ mt: 1.5, display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
        <Button variant="contained" startIcon={<SendIcon />} onClick={onSendReply}
                disabled={busy !== ""}>
          {busy === "reply" ? "Sending…" : "Send Reply"}
        </Button>
        <Button variant="outlined" size="small" component="label" startIcon={<AttachFileIcon />}>
          Attach
          <input type="file" multiple hidden
                 onChange={(e) => setReplyFiles([...replyFiles, ...Array.from(e.target.files)])} />
        </Button>
        {replyFiles.map((f, i) => (
          <Chip key={i} size="small" label={f.name}
                onDelete={() => setReplyFiles(replyFiles.filter((_, j) => j !== i))} />
        ))}
      </Box>
    </Box>
  );
}
