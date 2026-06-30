import { useLocation, useNavigate } from "react-router-dom";
import Button from "@mui/material/Button";
import ArrowBackIcon from "@mui/icons-material/ArrowBackIosNew";

// Universal Back button shown at the top-left of every page EXCEPT the Dashboard. Lives in the
// common Layout, so all current and future pages get it automatically. Goes to the previous page
// (navigate(-1)); when there is no in-app history (direct load / first entry) it falls back to
// the Dashboard.
export default function BackButton() {
  const navigate = useNavigate();
  const location = useLocation();

  // The Dashboard is the home page -- no Back button there.
  if (location.pathname === "/" || location.pathname.startsWith("/dashboard")) return null;

  function goBack() {
    // React Router gives the very first history entry the key "default"; in that case there is
    // no previous page to return to, so send the user to the Dashboard instead.
    if (location.key === "default") navigate("/dashboard");
    else navigate(-1);
  }

  return (
    <Button
      onClick={goBack}
      startIcon={<ArrowBackIcon sx={{ fontSize: 14 }} />}
      size="small"
      sx={{
        mb: 2, color: "text.secondary", textTransform: "none", fontWeight: 600,
        minWidth: 0, px: 1,
        "&:hover": { color: "primary.main", bgcolor: "action.hover" },
      }}
    >
      Back
    </Button>
  );
}
