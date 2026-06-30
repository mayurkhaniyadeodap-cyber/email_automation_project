import { api } from "../../api";

// Persist a partial BrandSettings patch. Creates the row if the brand has none yet.
export async function saveSettings(settings, brandId, patch) {
  if (settings?.id) {
    return api.patch(`/settings/${settings.id}/`, patch);
  }
  return api.post("/settings/", { brand: Number(brandId), ...patch });
}
