import { useEffect, useState } from "react";
import { api, getToken } from "../api";
import { fmtDate } from "./ui.jsx";

function humanSize(bytes) {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

const ICON = { image: "🖼", video: "🎬", file: "📄" };

export default function Attachments({ ticketId }) {
  const [items, setItems] = useState([]);
  const [preview, setPreview] = useState(null);

  useEffect(() => {
    api
      .get(`/tickets/${ticketId}/attachments/`)
      .then((d) => setItems(d.attachments || []))
      .catch(() => setItems([]));
  }, [ticketId]);

  const token = getToken();
  const fileUrl = (a, download = false) =>
    `${a.url}?token=${encodeURIComponent(token)}${download ? "&download=1" : ""}`;

  return (
    <div className="card">
      <h3>Attachments {items.length ? `(${items.length})` : ""}</h3>
      {items.length === 0 && <span className="muted">No attachments on this ticket.</span>}

      <div className="att-grid">
        {items.map((a, i) => (
          <div key={i} className="att-item">
            {a.downloadable && a.kind === "image" ? (
              <img
                className="att-thumb"
                src={fileUrl(a)}
                alt={a.filename}
                onClick={() => setPreview(fileUrl(a))}
                title="Click to enlarge"
              />
            ) : a.downloadable && a.kind === "video" ? (
              <video className="att-thumb" src={fileUrl(a)} controls preload="metadata" />
            ) : (
              <div className="att-icon">{ICON[a.kind] || ICON.file}</div>
            )}

            <div className="att-name" title={a.filename}>{a.filename}</div>
            <div className="att-meta muted">
              {(a.content_type || "unknown")} · {humanSize(a.size)}
              {a.created_at ? ` · ${fmtDate(a.created_at)}` : ""}
            </div>

            {a.downloadable ? (
              <a className="btn" href={fileUrl(a, true)} download>
                Download
              </a>
            ) : (
              <span className="muted" style={{ fontSize: 12 }}>Metadata only</span>
            )}
          </div>
        ))}
      </div>

      {preview && (
        <div className="att-lightbox" onClick={() => setPreview(null)}>
          <img src={preview} alt="preview" />
        </div>
      )}
    </div>
  );
}
