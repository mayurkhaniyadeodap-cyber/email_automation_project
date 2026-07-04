import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import { useScope } from "../scope.jsx";
import { useSupportEmails } from "../useSupportEmails.js";
import { PriorityBadge, StatusBadge, fmtDate } from "./ui.jsx";
import Attachments from "./Attachments.jsx";

// Agent-settable statuses (must match TicketViewSet.AGENT_SETTABLE_STATUSES).
const STATUS_OPTIONS = [
  { value: "awaiting_agent", label: "Awaiting Agent" },
  { value: "in_progress", label: "In Progress" },
  { value: "escalated", label: "Escalated" },
  { value: "resolved", label: "Resolved" },
  { value: "closed", label: "Closed" },
];

export default function TicketDetail() {
  const { id } = useParams();
  const { orgId, brandId } = useScope();
  const { emails: senders, defaultEmail } = useSupportEmails(orgId, brandId);
  const [ticket, setTicket] = useState(null);
  const [attachments, setAttachments] = useState(null);
  const [subTopics, setSubTopics] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [reply, setReply] = useState("");
  const [fromEmail, setFromEmail] = useState("");
  const [correctSub, setCorrectSub] = useState("");

  useEffect(() => { if (!fromEmail && defaultEmail) setFromEmail(defaultEmail); }, [defaultEmail, fromEmail]);

  async function load() {
    setError("");
    try {
      const t = await api.get(`/tickets/${id}/`);
      setTicket(t);
      setCorrectSub(t.sub_topic_ref ? String(t.sub_topic_ref) : "");
      api.get(`/tickets/${id}/attachments/`).then(setAttachments).catch(() => {});
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    if (brandId)
      api
        .get("/sub-topics/", { brand: brandId })
        .then((d) => setSubTopics(d.results || d))
        .catch(() => {});
  }, [brandId]);

  async function act(name, fn) {
    setBusy(name);
    setError("");
    try {
      await fn();
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  if (error && !ticket) return <div className="error">{error}</div>;
  if (!ticket) return <div className="muted">Loading…</div>;

  const sendReply = (isDraft) =>
    act(isDraft ? "draft" : "send", async () => {
      await api.post(`/tickets/${id}/reply/`,
        { body_text: reply, is_draft: isDraft, from_email: fromEmail });
      setReply("");
    });

  const setStatus = (status) =>
    act(`status:${status}`, () => api.post(`/tickets/${id}/set-status/`, { status }));

  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        {/* Care Panel (Gallabox) ticket number from store-json -- never the internal TKT id. */}
        <h3 style={{ margin: 0 }}>{ticket.ticket_number || "—"}</h3>
        <StatusBadge status={ticket.status} label={ticket.status_display} />
        <PriorityBadge priority={ticket.priority} label={ticket.priority_display} />
        {ticket.is_ignored && <span className="badge">ignored</span>}
      </div>

      {error && <div className="error">{error}</div>}

      <div style={{ display: "grid", gridTemplateColumns: "1.6fr 1fr", gap: 16 }}>
        {/* LEFT: thread + reply */}
        <div>
          <div className="card">
            <h3>{ticket.subject || "(no subject)"}</h3>
            {(ticket.messages || []).map((m) => (
              <div key={m.id} className={`msg ${m.direction}`}>
                <div className="meta">
                  {m.direction === "outbound" ? "↩ " : ""}
                  <b>{m.from_email || "—"}</b> → {m.to_email || "—"} ·{" "}
                  {fmtDate(m.created_at)}
                  {m.is_draft && <span className="badge" style={{ marginLeft: 6 }}>draft</span>}
                </div>
                <pre>{m.body_text || "(no text body)"}</pre>
              </div>
            ))}
          </div>

          {/* Attachments — below the conversation thread */}
          <Attachments ticketId={id} />

          <div className="card">
            <h3>Reply</h3>
            {senders.length > 0 && (
              <label className="row" style={{ gap: 6, alignItems: "center", marginBottom: 8 }}>
                <span className="muted" style={{ minWidth: 80 }}>Reply From</span>
                <select value={fromEmail} onChange={(e) => setFromEmail(e.target.value)}
                  style={{ minWidth: 260 }}>
                  {senders.map((s) => (
                    <option key={s.id} value={s.email}>
                      {s.email}{s.is_primary ? " (primary)" : ""}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <textarea
              placeholder="Type a reply to the customer…"
              value={reply}
              onChange={(e) => setReply(e.target.value)}
            />
            <div className="row" style={{ marginTop: 10 }}>
              <button
                className="btn primary"
                disabled={!reply.trim() || busy}
                onClick={() => sendReply(false)}
              >
                {busy === "send" ? "Sending…" : "Send"}
              </button>
              <button
                className="btn"
                disabled={!reply.trim() || busy}
                onClick={() => sendReply(true)}
              >
                Save as draft
              </button>
            </div>
          </div>
        </div>

        {/* RIGHT: status, classification, actions, details */}
        <div>
          <div className="card">
            <h3>Status</h3>
            <div className="row" style={{ marginBottom: 10 }}>
              <StatusBadge status={ticket.status} label={ticket.status_display} />
            </div>
            {/* Contextual next-step buttons follow the lifecycle. */}
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              {ticket.status === "awaiting_agent" && (
                <button className="btn primary" disabled={busy}
                  onClick={() => setStatus("in_progress")}>
                  {busy === "status:in_progress" ? "…" : "Start Work"}
                </button>
              )}
              {ticket.status === "in_progress" && (
                <button className="btn primary" disabled={busy}
                  onClick={() => setStatus("resolved")}>
                  {busy === "status:resolved" ? "…" : "Mark Resolved"}
                </button>
              )}
              {ticket.status === "resolved" && (
                <button className="btn primary" disabled={busy}
                  onClick={() => setStatus("closed")}>
                  {busy === "status:closed" ? "…" : "Close Ticket"}
                </button>
              )}
            </div>
            {/* Free-form status change. */}
            <div className="row" style={{ marginTop: 10 }}>
              <select
                value=""
                disabled={busy}
                onChange={(e) => e.target.value && setStatus(e.target.value)}
                style={{ flex: 1 }}
              >
                <option value="">Change status…</option>
                {STATUS_OPTIONS.filter((o) => o.value !== ticket.status).map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="card">
            <h3>AI &amp; actions</h3>
            <div className="kv" style={{ marginBottom: 12 }}>
              <span className="k">Category</span>
              <span>{ticket.category || "—"}</span>
              <span className="k">Sub-topic</span>
              <span>{ticket.sub_topic || "—"}</span>
              <span className="k">Confidence</span>
              <span>
                {ticket.ai_confidence != null ? ticket.ai_confidence.toFixed(2) : "—"}
              </span>
              <span className="k">Sentiment</span>
              <span>{ticket.sentiment || "—"}</span>
              <span className="k">Action taken</span>
              <span>{ticket.action_taken || "—"}</span>
            </div>
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              <button className="btn" disabled={busy} onClick={() =>
                act("classify", () => api.post(`/tickets/${id}/classify/`))}>
                {busy === "classify" ? "…" : "Classify"}
              </button>
              <button className="btn" disabled={busy} onClick={() =>
                act("decide", () => api.post(`/tickets/${id}/decide/`))}>
                {busy === "decide" ? "…" : "Run engine"}
              </button>
              {ticket.is_ignored ? (
                <button className="btn" disabled={busy} onClick={() =>
                  act("unignore", () => api.post(`/tickets/${id}/unignore/`))}>
                  Un-ignore
                </button>
              ) : (
                <button className="btn" disabled={busy} onClick={() =>
                  act("ignore", () => api.post(`/tickets/${id}/ignore/`, { reason: "Agent ignored" }))}>
                  Ignore
                </button>
              )}
            </div>
          </div>

          <div className="card">
            <h3>Reclassify</h3>
            <div className="row">
              <select
                value={correctSub}
                onChange={(e) => setCorrectSub(e.target.value)}
                style={{ flex: 1 }}
              >
                <option value="">Select sub-topic…</option>
                {subTopics.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.code} {s.name}
                  </option>
                ))}
              </select>
              <button
                className="btn"
                disabled={!correctSub || busy}
                onClick={() =>
                  act("correct", () =>
                    api.post(`/tickets/${id}/correct/`, { sub_topic_ref: Number(correctSub) })
                  )
                }
              >
                {busy === "correct" ? "…" : "Save"}
              </button>
            </div>
            <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>
              Logs a correction the AI-accuracy report counts.
            </div>
          </div>

          <div className="card">
            <h3>Details</h3>
            <div className="kv">
              {/* ORDER OWNER ALWAYS WINS: the customer is the verified order owner (name /
                  email / phone), never the email sender. The sender is shown separately,
                  for reply routing / history only. */}
              <span className="k">Customer Name</span>
              <span>{ticket.customer_name || "Unknown"}</span>
              <span className="k">Customer Email</span>
              <span>{ticket.customer_email || "—"}</span>
              <span className="k">Customer Phone</span>
              <span>{ticket.customer_phone || "—"}</span>
              <span className="k">Sender</span>
              <span>
                {ticket.sender_name
                  ? `${ticket.sender_name} <${ticket.sender_email || "—"}>`
                  : ticket.sender_email || "—"}
              </span>
              <span className="k">Mandatory inputs</span>
              <span>{(ticket.mandatory_inputs || []).join(", ") || "—"}</span>
              <span className="k">Extracted</span>
              <span>
                <code>{JSON.stringify(ticket.extracted || {})}</code>
              </span>
              <span className="k">SLA due</span>
              <span>{fmtDate(ticket.sla_due_at)}</span>
              <span className="k">Attachments</span>
              <span>
                {attachments
                  ? attachments.count === 0
                    ? "none"
                    : `${attachments.count} (${attachments.evidence.has_photo ? "photo " : ""}${attachments.evidence.has_unboxing_video ? "video" : ""})`
                  : "…"}
              </span>
            </div>
          </div>

          <div className="card">
            <h3>Audit log</h3>
            {(ticket.audit_log || []).map((a) => (
              <div key={a.id} style={{ fontSize: 13, marginBottom: 6 }}>
                <span className="muted">{fmtDate(a.created_at)}</span> ·{" "}
                {a.event === "status_changed" ? (
                  <span>
                    Status changed:{" "}
                    <b>{a.detail?.from_label || a.detail?.from}</b> →{" "}
                    <b>{a.detail?.to_label || a.detail?.to}</b>
                  </span>
                ) : (
                  <b>{a.event}</b>
                )}{" "}
                <span className="muted">by {a.actor}</span>
              </div>
            ))}
            {(ticket.audit_log || []).length === 0 && (
              <span className="muted">No events.</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
