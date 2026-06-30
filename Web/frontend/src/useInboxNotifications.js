// Visual-only notifications for new HIGH-priority Escalations AND new Internal Communications.
//
// - Polls each module's /<module>/unread_count/ every POLL_MS (20s) for the selected org/brand.
// - Exposes per-module unread `counts` (sidebar badges) and their `total` (browser title).
// - Emits ONE toast per NEW item (deduped by id via `seen`), shown sequentially from a queue.
// - NEVER plays a sound (the toast is purely visual).
//
// `modules`: [{ key, path, label, route }]
//   key   – stable id used in counts + the seen-set namespace
//   path  – API base, e.g. "/escalations" (endpoint = `${path}/unread_count/`)
//   label – human label for the toast ("Escalation" / "Internal Communication")
//   route – SPA route to open the item, deep-linked as `${route}?open=<id>`

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

const POLL_MS = 20000; // 20s.

export function useInboxNotifications(orgId, brandId, modules, enabled = true) {
  const [counts, setCounts] = useState({});   // { [key]: number }
  const [toast, setToast] = useState(null);   // current toast item, or null
  const seen = useRef(new Set());             // `${key}:${id}` already notified (one toast per item)
  const queue = useRef([]);                   // pending toast items
  const baselined = useRef(false);            // first poll only seeds `seen` (no toast for backlog)

  // Show the next queued toast if none is currently visible.
  const showNext = useCallback(() => {
    setToast((cur) => (cur ? cur : queue.current.shift() || null));
  }, []);

  const dismiss = useCallback(() => {
    setToast(null);
    // Let state settle, then surface the next queued item (if any).
    setTimeout(showNext, 0);
  }, [showNext]);

  const refresh = useCallback(async () => {
    if (!enabled || !brandId || !modules.length) return;
    const results = await Promise.all(modules.map(async (m) => {
      try {
        const res = await api.get(`${m.path}/unread_count/`, { organization: orgId, brand: brandId });
        return { m, count: Number(res?.count || 0), items: res?.items || [] };
      } catch {
        return null; // transient error — keep last known count, retry next tick
      }
    }));

    const fresh = {};
    const newItems = [];
    for (const r of results) {
      if (!r) continue;
      fresh[r.m.key] = r.count;
      // Endpoint returns newest-first; reverse so the queue surfaces oldest-unseen first.
      for (const it of [...r.items].reverse()) {
        const sid = `${r.m.key}:${it.id}`;
        if (seen.current.has(sid)) continue;
        seen.current.add(sid);
        if (baselined.current) {
          newItems.push({ ...it, key: r.m.key, label: r.m.label, route: r.m.route });
        }
      }
    }
    setCounts((prev) => ({ ...prev, ...fresh }));

    if (!baselined.current) { baselined.current = true; return; } // seed only on first poll
    if (newItems.length) {
      queue.current.push(...newItems);
      showNext();
    }
  }, [enabled, orgId, brandId, modules, showNext]);

  // Ask for browser-notification permission once (badges/toasts work regardless of the answer).
  useEffect(() => {
    if (enabled && typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
  }, [enabled]);

  // Reset all state when the scope changes so switching brand never toasts an existing backlog.
  useEffect(() => {
    baselined.current = false;
    seen.current = new Set();
    queue.current = [];
    setToast(null);
    setCounts({});
  }, [orgId, brandId]);

  // Poll immediately, then on an interval; also refresh when the tab regains focus.
  useEffect(() => {
    if (!enabled || !brandId || !modules.length) { setCounts({}); return undefined; }
    refresh();
    const id = setInterval(refresh, POLL_MS);
    const onVis = () => { if (!document.hidden) refresh(); };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [enabled, brandId, modules, refresh]);

  const total = Object.values(counts).reduce((a, b) => a + (b || 0), 0);

  // Fire a silent browser notification for the visible toast when the tab is hidden.
  useEffect(() => {
    if (!toast || !document.hidden) return;
    if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
    try {
      // eslint-disable-next-line no-new
      new Notification(`New ${toast.label} — DeoDap Care`, {
        body: `${toast.sender_name || toast.sender || "Unknown"}\n${toast.subject || "(no subject)"}`,
        tag: `deodap-${toast.key}-${toast.id}`,
        silent: true, // never play a sound
      });
    } catch {
      /* notifications unsupported / blocked — the visual toast + badge still apply */
    }
  }, [toast]);

  return { counts, total, toast, dismiss, refresh };
}
