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
import ListItemIcon from "@mui/material/ListItemIcon";
import ListItemText from "@mui/material/ListItemText";
import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";
import Snackbar from "@mui/material/Snackbar";
import Toolbar from "@mui/material/Toolbar";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import DashboardIcon from "@mui/icons-material/DashboardOutlined";
import InboxIcon from "@mui/icons-material/MailOutlined";
import ComposeIcon from "@mui/icons-material/EditOutlined";
import TicketIcon from "@mui/icons-material/ConfirmationNumberOutlined";
import EscalationIcon from "@mui/icons-material/ReportProblemOutlined";
import InternalCommsIcon from "@mui/icons-material/ForumOutlined";
import SettingsIcon from "@mui/icons-material/SettingsOutlined";
import LogoutIcon from "@mui/icons-material/Logout";
import RefreshIcon from "@mui/icons-material/Refresh";

import { api } from "../api";
import { useAuth } from "../auth.jsx";
import { useScope } from "../scope.jsx";
import { useInboxNotifications } from "../useInboxNotifications.js";
import BackButton from "./BackButton.jsx";

const BASE_TITLE = "DeoDap Care Panel";

// Modules with live unread badges + new-item toasts. `navKey` ties a module to its sidebar item;
// `path` is the API base; `route` is the SPA page (deep-linked as `${route}?open=<id>`).
const NOTIFY_MODULES = [
  { key: "escalations", navKey: "escalations", path: "/escalations",
    label: "Escalation", route: "/escalations" },
  { key: "internal", navKey: "internal-communications", path: "/internal-emails",
    label: "Internal Communication", route: "/internal-communications" },
];

const DRAWER_WIDTH = 220;
const NAV = [
  ["dashboard", "/dashboard", "Dashboard", <DashboardIcon />],
  ["inbox", "/inbox", "Inbox", <InboxIcon />],
  ["compose", "/compose", "Compose", <ComposeIcon />],
  ["tickets", "/tickets", "Tickets", <TicketIcon />],
  ["escalations", "/escalations", "Escalation", <EscalationIcon />],
  ["internal-communications", "/internal-communications", "Internal Communications", <InternalCommsIcon />],
  ["settings", "/settings", "Settings", <SettingsIcon />],
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

  // Visual notifications for new Escalations + Internal Communications. Only poll the modules the
  // role is actually allowed to see (no wasted requests / no badges they can't open).
  const modules = useMemo(
    () => NOTIFY_MODULES.filter((m) => allowedNav.includes(m.navKey)),
    [allowedNav]);
  const { counts, total, toast, dismiss, refresh: refreshNotifications } =
    useInboxNotifications(orgId, brandId, modules, modules.length > 0);

  // Update the browser tab title with the TOTAL unread from both modules, e.g. "(5) DeoDap Care Panel".
  useEffect(() => {
    document.title = total > 0 ? `(${total}) ${BASE_TITLE}` : BASE_TITLE;
    return () => { document.title = BASE_TITLE; };
  }, [total]);

  // Redirect away from pages the role can't access (e.g. agent on /settings).
  // `pending` / `escalations` are reachable from dashboard cards even though they have no
  // sidebar item.
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

  return (
    <Box sx={{ display: "flex" }}>
      <AppBar position="fixed" sx={{ zIndex: (t) => t.zIndex.drawer + 1 }}>
        <Toolbar>
          <Typography variant="h6" sx={{ mr: 4 }}>
            DeoDap Care Panel
          </Typography>
          <Box sx={{ flexGrow: 1 }} />

          {orgs?.length > 1 && (
            <Select
              size="small" variant="standard" disableUnderline
              value={orgId} onChange={(e) => selectOrg(e.target.value)}
              sx={{ color: "#fff", mr: 2, "& .MuiSvgIcon-root": { color: "#fff" } }}
            >
              {orgs.map((o) => <MenuItem key={o.id} value={o.id}>{o.name}</MenuItem>)}
            </Select>
          )}
          {brands?.length > 1 && (
            <Select
              size="small" variant="standard" disableUnderline
              value={brandId} onChange={(e) => selectBrand(e.target.value)}
              sx={{ color: "#fff", mr: 2, "& .MuiSvgIcon-root": { color: "#fff" } }}
            >
              {brands.map((b) => <MenuItem key={b.id} value={b.id}>{b.name}</MenuItem>)}
            </Select>
          )}

          {!readOnly && (
            <Button
              color="inherit" startIcon={<RefreshIcon />}
              onClick={fetchMail} disabled={fetching}
            >
              {fetching ? "Fetching…" : "Fetch Mail"}
            </Button>
          )}
          {!user?.auto_login && (
            <Tooltip title="Logout">
              <IconButton color="inherit" onClick={logout}><LogoutIcon /></IconButton>
            </Tooltip>
          )}
        </Toolbar>
      </AppBar>

      <Drawer
        variant="permanent"
        sx={{
          width: DRAWER_WIDTH, flexShrink: 0,
          "& .MuiDrawer-paper": { width: DRAWER_WIDTH, boxSizing: "border-box" },
        }}
      >
        <Toolbar />
        <List>
          {navItems.map(([key, to, label, icon]) => {
            // Map the sidebar item to its module's live unread count (0 = no badge).
            const mod = NOTIFY_MODULES.find((m) => m.navKey === key);
            const unread = mod ? (counts[mod.key] || 0) : 0;
            return (
              <ListItemButton
                key={key}
                component={NavLink}
                to={to}
                selected={location.pathname.startsWith(to)}
              >
                <ListItemIcon sx={{ minWidth: 40 }}>{icon}</ListItemIcon>
                <ListItemText primary={label} />
                {unread > 0 && (
                  <Badge badgeContent={unread} color="error" max={99}
                    sx={{ mr: 1.5, "& .MuiBadge-badge": { position: "static", transform: "none" } }} />
                )}
              </ListItemButton>
            );
          })}
        </List>
      </Drawer>

      <Box component="main" sx={{ flexGrow: 1, p: 3, minHeight: "100vh" }}>
        <Toolbar />
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

      {/* New Escalation / Internal Communication toast (top-right, visual only — no sound).
          Shows sender name + email + subject; click opens the exact item. */}
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
            icon={toast.key === "escalations"
              ? <EscalationIcon fontSize="inherit" />
              : <InternalCommsIcon fontSize="inherit" />}
            sx={{ cursor: "pointer", boxShadow: 3, maxWidth: 360,
              ...(toast.key === "internal" ? { bgcolor: "#5e35b1" } : {}) }}
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
