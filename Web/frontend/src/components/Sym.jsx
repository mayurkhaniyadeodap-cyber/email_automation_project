import Box from "@mui/material/Box";

// Google Material Symbols Outlined icon (font loaded in index.html). The ONLY icon set used.
export default function Sym({ name, size = 22, color = "inherit", weight = 500, fill = 0, sx }) {
  return (
    <Box
      component="span"
      className="material-symbols-outlined"
      sx={{
        fontSize: size, color, lineHeight: 1, userSelect: "none",
        fontVariationSettings: `'FILL' ${fill}, 'wght' ${weight}, 'GRAD' 0, 'opsz' 24`,
        ...sx,
      }}
    >
      {name}
    </Box>
  );
}
