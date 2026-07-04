import { Navigate, Route, Routes } from "react-router-dom";
import Box from "@mui/material/Box";
import CircularProgress from "@mui/material/CircularProgress";
import { useAuth } from "./auth.jsx";
import { ScopeProvider } from "./scope.jsx";
import Layout from "./components/Layout.jsx";
import Dashboard from "./components/Dashboard.jsx";
import Inbox from "./components/Inbox.jsx";
import Compose from "./components/Compose.jsx";
import Pending from "./components/Pending.jsx";
import PendingDetail from "./components/PendingDetail.jsx";
import Escalations from "./components/Escalations.jsx";
import InternalComms from "./components/InternalComms.jsx";
import Reports from "./components/Reports.jsx";
import TicketList from "./components/TicketList.jsx";
import TicketDetail from "./components/TicketDetail.jsx";
import Settings from "./components/Settings.jsx";

export default function App() {
  const { user, loading } = useAuth();

  // No login screen: the panel auto-authenticates (AUTO_LOGIN). While that resolves -- or if
  // it briefly fails -- show a lightweight connecting state with a retry, never a sign-in form.
  if (loading || !user) {
    return (
      <Box sx={{ display: "flex", flexDirection: "column", alignItems: "center",
                 justifyContent: "center", gap: 2, mt: 10 }}>
        <CircularProgress />
        <span style={{ color: "#667" }}>Connecting to the Care Panel…</span>
        {!loading && !user && (
          <button className="btn" onClick={() => window.location.reload()}>Retry</button>
        )}
      </Box>
    );
  }

  return (
    <ScopeProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/inbox" element={<Inbox />} />
          <Route path="/compose" element={<Compose />} />
          <Route path="/pending" element={<Pending />} />
          <Route path="/pending/:id" element={<PendingDetail />} />
          <Route path="/escalations" element={<Escalations />} />
          <Route path="/internal-communications" element={<InternalComms />} />
          <Route path="/reports/:kind" element={<Reports />} />
          <Route path="/tickets" element={<TicketList />} />
          <Route path="/tickets/:id" element={<TicketDetail />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Route>
      </Routes>
    </ScopeProvider>
  );
}
