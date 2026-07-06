import { useEffect, useRef, useState } from "react";
import Autocomplete from "@mui/material/Autocomplete";
import Box from "@mui/material/Box";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import { api } from "../api";

// One reusable autocomplete for EVERY search box in the app. Backed by the single
// /search/suggest/ endpoint. Props:
//   value      - controlled input text (the page's `search` state)
//   onChange   - (text) => void: keep the page's search state in sync while typing
//   onSearch   - (text) => void: RUN the search (fires on Enter or when a suggestion is picked)
//   placeholder, sx, types (optional CSV to restrict suggestion kinds), orgId/brandId (scope)
//
// Behaviour required by the spec is delegated to MUI Autocomplete (keyboard up/down, Enter, Esc,
// click-outside) plus: suggestions after 2 chars, <=10 results, case-insensitive highlight,
// debounced (<200ms) fetch, freeSolo so plain-Enter search still works.

const MIN_CHARS = 2;
const DEBOUNCE_MS = 150;

const TYPE_LABEL = {
  customer: "Customer", email: "Email", subject: "Subject", ticket: "Ticket No.",
  order: "Order", phone: "Phone", tracking: "Tracking", category: "Category",
  subtopic: "Sub-category", status: "Status", priority: "Priority",
  assignee: "Assignee", company: "Company",
};

// Case-insensitive highlight of the first match of `q` inside `text`.
function highlight(text, q) {
  const s = String(text ?? "");
  const query = (q || "").trim();
  if (!query) return s;
  const i = s.toLowerCase().indexOf(query.toLowerCase());
  if (i < 0) return s;
  return (
    <span>
      {s.slice(0, i)}
      <strong>{s.slice(i, i + query.length)}</strong>
      {s.slice(i + query.length)}
    </span>
  );
}

export default function SearchAutocomplete({
  value, onChange, onSearch, placeholder = "Search…", sx,
  types, orgId, brandId, size = "small",
}) {
  const [options, setOptions] = useState([]);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);

  // Debounced server-side suggestions once the query reaches 2 characters.
  useEffect(() => {
    const term = (value || "").trim();
    if (term.length < MIN_CHARS) { setOptions([]); setLoading(false); return; }
    const mySeq = ++seq.current;
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        const params = { q: term };
        if (types) params.types = types;
        if (orgId) params.organization = orgId;
        if (brandId) params.brand = brandId;
        const data = await api.get("/search/suggest/", params);
        if (mySeq === seq.current) setOptions((data.suggestions || []).slice(0, 10));
      } catch {
        if (mySeq === seq.current) setOptions([]);
      } finally {
        if (mySeq === seq.current) setLoading(false);
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [value, types, orgId, brandId]);

  return (
    <Autocomplete
      freeSolo
      size={size}
      sx={sx}
      options={options}
      loading={loading}
      filterOptions={(x) => x}                 // server already filtered; don't re-filter
      inputValue={value || ""}
      getOptionLabel={(o) => (typeof o === "string" ? o : o?.value || "")}
      isOptionEqualToValue={(o, v) => (o?.value ?? o) === (v?.value ?? v)}
      onInputChange={(_e, v) => onChange(v)}   // keep page search state in sync (typing + select)
      onChange={(_e, picked) => {
        // Fires when a suggestion is picked (option) AND on plain Enter (freeSolo -> the typed
        // string). Both keep Enter working exactly as before, and run the search.
        const text = picked == null ? "" : (typeof picked === "string" ? picked : picked.value);
        onChange(text);
        onSearch(text);
      }}
      renderInput={(params) => (
        // Spread params straight through -- MUI v9 exposes input props via slotProps, not
        // params.InputProps, so we must NOT reach into it. The `loading` prop above already
        // renders a loading indicator in the dropdown.
        <TextField {...params} placeholder={placeholder} />
      )}
      renderOption={(props, option) => {
        const { key, ...rest } = props;
        return (
          <li key={key} {...rest}>
            <Box sx={{ display: "flex", justifyContent: "space-between",
                       alignItems: "center", width: "100%", gap: 1 }}>
              <span>{highlight(option.value, value)}</span>
              <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>
                {TYPE_LABEL[option.type] || option.type}
              </Typography>
            </Box>
          </li>
        );
      }}
    />
  );
}
