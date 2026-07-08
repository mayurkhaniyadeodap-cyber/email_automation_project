import { useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import AppBar from "@mui/material/AppBar";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Drawer from "@mui/material/Drawer";
import IconButton from "@mui/material/IconButton";
import Alert from "@mui/material/Alert";
import Badge from "@mui/material/Badge";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";
import Snackbar from "@mui/material/Snackbar";
import Toolbar from "@mui/material/Toolbar";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import { api } from "../api";
import { useAuth } from "../auth.jsx";
import { useScope } from "../scope.jsx";
import { useInboxNotifications } from "../useInboxNotifications.js";
import BackButton from "./BackButton.jsx";
import Sym from "./Sym.jsx";

const BASE_TITLE = "DeoDap Care Panel";
const BLUE = "#2563eb";

const NOTIFY_MODULES = [
  { key: "escalations", navKey: "escalations", path: "/escalations",
    label: "Escalation", route: "/escalations" },
  { key: "internal", navKey: "internal-communications", path: "/internal-emails",
    label: "Internal Communication", route: "/internal-communications" },
];

const DRAWER_WIDTH = 236;
// [navKey, route, label, Material Symbol name]
const NAV = [
  ["dashboard", "/dashboard", "Dashboard", "dashboard"],
  ["inbox", "/inbox", "Inbox", "inbox"],
  ["compose", "/compose", "Compose", "edit_square"],
  ["tickets", "/tickets", "Tickets", "confirmation_number"],
  ["escalations", "/escalations", "Escalation", "warning"],
  ["internal-communications", "/internal-communications", "Internal Communications", "forum"],
  ["settings", "/settings", "Settings", "settings"],
];

export default function Layout() {
  const { user, logout } = useAuth();
  const { orgId, brandId, orgs, brands, selectOrg, selectBrand } = useScope();
  const navigate = useNavigate();
  const location = useLocation();
  const [refreshKey, setRefreshKey] = useState(0);
  const [snack, setSnack] = useState("");
  const [fetching, setFetching] = useState(false);

  const allowedNav = user?.permissions?.nav || ["inbox", "tickets"];
  const readOnly = !!user?.permissions?.read_only;
  const navItems = NAV.filter(([key]) => allowedNav.includes(key));

  const modules = useMemo(
    () => NOTIFY_MODULES.filter((m) => allowedNav.includes(m.navKey)),
    [allowedNav]);
  const { counts, total, toast, dismiss, refresh: refreshNotifications } =
    useInboxNotifications(orgId, brandId, modules, modules.length > 0);

  useEffect(() => {
    document.title = total > 0 ? `(${total}) ${BASE_TITLE}` : BASE_TITLE;
    return () => { document.title = BASE_TITLE; };
  }, [total]);

  const EXTRA_ALLOWED = ["pending", "escalations", "internal-communications", "reports"];
  useEffect(() => {
    if (!user) return;
    const top = location.pathname.split("/")[1] || "";
    if (top && ![...allowedNav, ...EXTRA_ALLOWED].includes(top))
      navigate(`/${allowedNav[0]}`, { replace: true });
  }, [user, location.pathname]); // eslint-disable-line

  const [mailboxId, setMailboxId] = useState(null);
  useEffect(() => {
    if (!brandId) return;
    api.get("/mailboxes/", { brand: brandId }).then((d) => {
      const list = d.results || d;
      setMailboxId(list[0]?.id || null);
    });
  }, [brandId]);

  async function fetchMail() {
    setFetching(true);
    try {
      const q = mailboxId ? `?mailbox=${mailboxId}` : "";
      const res = await api.post(`/gmail/fetch/${q}`);
      const n = res.fetched ?? res.ingested ?? 0;
      setSnack(n === 0 ? "No new emails." : `Fetched ${n} new email${n === 1 ? "" : "s"}.`);
      setRefreshKey((k) => k + 1);
    } catch (err) {
      setSnack(err.data?.detail || err.message || "Fetch failed");
    } finally {
      setFetching(false);
    }
  }

  const selectSx = {
    minWidth: 130, mr: 1, "& .MuiSelect-select": { py: 0.75, fontSize: 14, color: "#334155" },
    "& fieldset": { borderColor: "#e5e9f0" },
  };

  return (
    <Box sx={{ display: "flex", bgcolor: "#F8FAFC", minHeight: "100vh" }}>
      <AppBar
        position="fixed"
        elevation={0}
        sx={{
          zIndex: (t) => t.zIndex.drawer + 1, bgcolor: "#fff", color: "#0f172a",
          borderBottom: "1px solid #e5e9f0",
        }}
      >
        <Toolbar sx={{ minHeight: 64 }}>
          {/* Left: brand */}
          <Box sx={{ display: "flex", alignItems: "baseline", gap: 1 }}>
            <Typography sx={{ fontWeight: 800, fontSize: 21, letterSpacing: -0.3 }}>
              <Box component="span" sx={{ color: "#e11d48" }}>Deo</Box>
              <Box component="span" sx={{ color: "#0f172a" }}>Dap</Box>
            </Typography>
            <Typography sx={{ color: "#64748b", fontSize: 15, fontWeight: 600 }}>Care Panel</Typography>
          </Box>

          <Box sx={{ flexGrow: 1 }} />

          {/* Right: scope selectors (only when multiple) + Refresh + Fetch Mail */}
          {orgs?.length > 1 && (
            <Select size="small" value={orgId} onChange={(e) => selectOrg(e.target.value)} sx={selectSx}>
              {orgs.map((o) => <MenuItem key={o.id} value={o.id}>{o.name}</MenuItem>)}
            </Select>
          )}
          {brands?.length > 1 && (
            <Select size="small" value={brandId} onChange={(e) => selectBrand(e.target.value)} sx={selectSx}>
              {brands.map((b) => <MenuItem key={b.id} value={b.id}>{b.name}</MenuItem>)}
            </Select>
          )}

          <Tooltip title="Refresh">
            <IconButton
              onClick={() => { setRefreshKey((k) => k + 1); refreshNotifications?.(); }}
              sx={{ color: "#475569", mr: 0.5 }}
            >
              <Sym name="refresh" size={22} />
            </IconButton>
          </Tooltip>

          {!readOnly && (
            <Button
              variant="contained"
              startIcon={<Sym name="mail" size={18} />}
              onClick={fetchMail}
              disabled={fetching}
              sx={{
                bgcolor: BLUE, borderRadius: "10px", textTransform: "none", fontWeight: 600,
                boxShadow: "none", px: 2, "&:hover": { bgcolor: "#1d4ed8", boxShadow: "none" },
              }}
            >
              {fetching ? "Fetching…" : "Fetch Mail"}
            </Button>
          )}
          {!user?.auto_login && (
            <Tooltip title="Logout">
              <IconButton onClick={logout} sx={{ color: "#94a3b8", ml: 0.5 }}>
                <Sym name="logout" size={20} />
              </IconButton>
            </Tooltip>
          )}
        </Toolbar>
      </AppBar>

      <Drawer
        variant="permanent"
        sx={{
          width: DRAWER_WIDTH, flexShrink: 0,
          "& .MuiDrawer-paper": {
            width: DRAWER_WIDTH, boxSizing: "border-box", bgcolor: "#fff",
            borderRight: "1px solid #e5e9f0",
          },
        }}
      >
        <Toolbar sx={{ minHeight: 64 }} />
        <List sx={{ px: 1.25, pt: 1.5 }}>
          {navItems.map(([key, to, label, icon]) => {
            const mod = NOTIFY_MODULES.find((m) => m.navKey === key);
            const unread = mod ? (counts[mod.key] || 0) : 0;
            const active = location.pathname.startsWith(to);
            return (
              <ListItemButton
                key={key}
                component={NavLink}
                to={to}
                disableRipple
                sx={{
                  borderRadius: "10px", mb: 0.5, py: 1,
                  color: active ? BLUE : "#475569",
                  bgcolor: active ? "rgba(37,99,235,.10)" : "transparent",
                  "&:hover": { bgcolor: active ? "rgba(37,99,235,.14)" : "#f1f5f9" },
                  "&.active": { bgcolor: "rgba(37,99,235,.10)" },
                }}
              >
                <Box sx={{ minWidth: 34, display: "flex", alignItems: "center" }}>
                  <Sym name={icon} size={22} weight={active ? 600 : 500}
                    fill={active ? 1 : 0} color={active ? BLUE : "#64748b"} />
                </Box>
                <ListItemText
                  primary={label}
                  primaryTypographyProps={{ fontSize: 14.5, fontWeight: active ? 700 : 500 }}
                />
                {unread > 0 && (
                  <Badge badgeContent={unread} color="error" max={99}
                    sx={{ mr: 1.5, "& .MuiBadge-badge": { position: "static", transform: "none" } }} />
                )}
              </ListItemButton>
            );
          })}
        </List>
      </Drawer>

      <Box component="main" sx={{ flexGrow: 1, p: 3, minHeight: "100vh", bgcolor: "#F8FAFC" }}>
        <Toolbar sx={{ minHeight: 64 }} />
        <BackButton />
        <Outlet context={{ refreshKey, brandId, orgId, refreshNotifications }} />
      </Box>

      <Snackbar
        open={!!snack}
        autoHideDuration={4000}
        onClose={() => setSnack("")}
        message={snack}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      />

      <Snackbar
        open={!!toast}
        autoHideDuration={9000}
        onClose={(_e, reason) => { if (reason !== "clickaway") dismiss(); }}
        anchorOrigin={{ vertical: "top", horizontal: "right" }}
      >
        {toast ? (
          <Alert
            onClose={dismiss}
            severity={toast.key === "escalations" ? "error" : "info"}
            variant="filled"
            icon={<Sym name={toast.key === "escalations" ? "warning" : "forum"} size={20} color="#fff" />}
            sx={{
              cursor: "pointer", boxShadow: 3, maxWidth: 360,
              ...(toast.key === "internal" ? { bgcolor: "#5e35b1" } : {}),
            }}
            onClick={() => {
              const t = toast;
              dismiss();
              navigate(`${t.route}?open=${t.id}`);
              refreshNotifications?.();
            }}
          >
            <Typography variant="subtitle2" sx={{ fontWeight: 700, lineHeight: 1.2 }}>
              New {toast.label}
            </Typography>
            <Typography variant="body2" sx={{ fontWeight: 600 }} noWrap>
              {toast.sender_name || toast.sender || "Unknown"}
            </Typography>
            {toast.sender && (
              <Typography variant="caption" sx={{ display: "block", opacity: 0.9 }} noWrap>
                {toast.sender}
              </Typography>
            )}
            <Typography variant="caption" sx={{ display: "block", opacity: 0.95 }} noWrap>
              {toast.subject || "(no subject)"}
            </Typography>
            <Typography variant="caption" sx={{ display: "block", mt: 0.5, opacity: 0.85 }}>
              Click to open
            </Typography>
          </Alert>
        ) : <span />}
      </Snackbar>
    </Box>
  );
}
