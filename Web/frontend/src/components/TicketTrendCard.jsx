import { useEffect, useRef, useState } from "react";
import Box from "@mui/material/Box";
import CircularProgress from "@mui/material/CircularProgress";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Select from "@mui/material/Select";
import Typography from "@mui/material/Typography";
import {
  CategoryScale, Chart, LinearScale, LineElement, PointElement, Tooltip,
} from "chart.js";
import { Line } from "react-chartjs-2";
import { api } from "../api";

Chart.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip);

const BLUE = "#2563eb";
const CARD = {
  p: 3, borderRadius: "16px", bgcolor: "#fff", border: "1px solid #eef1f5",
  boxShadow: "0 1px 2px rgba(16,24,40,.04), 0 1px 3px rgba(16,24,40,.06)",
};
const TITLES = { week: "Last 7 Days", month: "Last 30 Days", year: "Last 12 Months" };

export default function TicketTrendCard({ orgId, brandId, refreshKey }) {
  const [range, setRange] = useState("week");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const cacheRef = useRef({});   // keyed by brand:refreshKey:range -> {labels, values}

  useEffect(() => {
    if (!brandId) return;
    // Fetch ONLY when the range (or brand / manual refresh) changes; cached ranges load instantly.
    const key = `${brandId}:${refreshKey}:${range}`;
    if (cacheRef.current[key]) { setData(cacheRef.current[key]); setLoading(false); return; }
    let alive = true;
    setLoading(true);
    setData(null);
    api.get("/dashboard/ticket-trend", { range, organization: orgId, brand: brandId })
      .then((r) => {
        const d = { labels: r?.labels || [], values: r?.values || [] };
        cacheRef.current[key] = d;
        if (alive) setData(d);
      })
      .catch(() => { if (alive) setData({ labels: [], values: [] }); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [range, orgId, brandId, refreshKey]);

  const hasData = !!data && data.values.some((v) => v > 0);

  const chartData = {
    labels: data?.labels || [],
    datasets: [{
      label: "Tickets",
      data: data?.values || [],
      borderColor: BLUE,
      backgroundColor: BLUE,
      borderWidth: 2.5,
      tension: 0.35,
      fill: false,
      pointRadius: 3,
      pointHoverRadius: 5,
      pointBackgroundColor: "#fff",
      pointBorderColor: BLUE,
      pointBorderWidth: 2,
    }],
  };

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 550, easing: "easeOutQuart" },
    interaction: { intersect: false, mode: "index" },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: "#0f172a", padding: 10, cornerRadius: 8, displayColors: false,
        callbacks: { label: (c) => ` ${c.parsed.y} ticket${c.parsed.y === 1 ? "" : "s"}` },
      },
    },
    scales: {
      x: {
        grid: { display: false }, border: { display: false },
        ticks: { color: "#94a3b8", maxRotation: 0, autoSkip: true, maxTicksLimit: 12 },
      },
      y: {
        beginAtZero: true, grid: { color: "#eef1f5" }, border: { display: false },
        ticks: { color: "#94a3b8", precision: 0 },
      },
    },
  };

  return (
    <Paper elevation={0} sx={CARD}>
      <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "center", mb: 2 }}>
        <Typography sx={{ fontSize: 16, fontWeight: 700, color: "#0f172a" }}>
          Ticket Trend – {TITLES[range]}
        </Typography>
        <Select
          size="small"
          value={range}
          onChange={(e) => setRange(e.target.value)}
          sx={{
            minWidth: 118, "& .MuiSelect-select": { py: 0.75, fontSize: 14, color: "#334155" },
            "& fieldset": { borderColor: "#e5e9f0" },
          }}
        >
          <MenuItem value="week">Week</MenuItem>
          <MenuItem value="month">Month</MenuItem>
          <MenuItem value="year">Year</MenuItem>
        </Select>
      </Box>

      <Box sx={{ position: "relative", height: 280 }}>
        {loading && (
          <Box sx={{
            position: "absolute", inset: 0, display: "grid", placeItems: "center",
            bgcolor: "rgba(255,255,255,.6)", zIndex: 2, borderRadius: "12px",
          }}>
            <CircularProgress size={28} />
          </Box>
        )}
        {!loading && !hasData ? (
          <Box sx={{ height: "100%", display: "grid", placeItems: "center" }}>
            <Typography sx={{ fontSize: 14, color: "#94a3b8" }}>
              No ticket data available for the selected period.
            </Typography>
          </Box>
        ) : (
          <Line data={chartData} options={options} />
        )}
      </Box>
    </Paper>
  );
}
