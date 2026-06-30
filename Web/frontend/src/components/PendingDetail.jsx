import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { api } from "../api";
import { fmtDate } from "./chips.jsx";

// Read-only view of a HELD conversation (PendingConversation) -- an email that was
// fetched and processed but has no Ticket yet (e.g. verification failed / awaiting
// evidence). It has no Message thread, so we render its stored body + the auto-replies
// the system sent. This is what makes a "could not verify" email visible & inspectable.
export default function PendingDetail() {
  const { id } = useParams();
  const [p, setP] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await api.get(`/pending/${id}/`);
        if (alive) setP(data);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [id]);

  if (loading) return <Typography sx={{ p: 3 }}>Loading…</Typography>;
  if (!p) return <Typography sx={{ p: 3 }}>Conversation not found.</Typography>;

  const replyNote = p.status === "awaiting_evidence" || p.requires_evidence
    ? "Auto-reply sent: verification / evidence request"
    : "Auto-reply sent";

  return (
    <Box>

      <Paper variant="outlined" sx={{ p: 3, mb: 2 }}>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1, flexWrap: "wrap" }}>
          <Typography variant="h6">{p.subject || "(no subject)"}</Typography>
          <Chip size="small" color="warning" label="HELD — no ticket yet" />
          <Chip size="small" variant="outlined" label={p.status || "pending"} />
        </Stack>
        <Typography variant="body2" color="text.secondary">
          From: {p.customer_email || "—"}
          {p.phone ? ` · ${p.phone}` : ""}
          {p.order_id ? ` · order ${p.order_id}` : ""}
          {" · "}{fmtDate(p.created_at)}
        </Typography>
        {p.issue_summary && (
          <Typography variant="body2" sx={{ mt: 1 }}>
            <b>Issue:</b> {p.issue_summary}
          </Typography>
        )}
      </Paper>

      <Paper variant="outlined" sx={{ p: 3, mb: 2 }}>
        <Typography variant="subtitle2" sx={{ mb: 1 }}>Customer email</Typography>
        <Typography component="pre" sx={{ whiteSpace: "pre-wrap", fontFamily: "inherit", m: 0 }}>
          {p.body_text || "(empty body)"}
        </Typography>
      </Paper>

      <Paper variant="outlined" sx={{ p: 3 }}>
        <Typography variant="subtitle2" sx={{ mb: 1 }}>System activity</Typography>
        <Typography variant="body2" color="text.secondary">
          {replyNote} · {p.evidence_requests || 0} time(s).
        </Typography>
        <Divider sx={{ my: 1.5 }} />
        <Typography variant="caption" color="text.secondary">
          This conversation is held until the customer is verified (or sends evidence).
          It becomes a full ticket — and is pushed to the Care Panel — only after that.
        </Typography>
      </Paper>
    </Box>
  );
}
