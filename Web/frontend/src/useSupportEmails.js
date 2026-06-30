import { useEffect, useState } from "react";
import { api } from "./api";

// Active support emails for the brand (the primary inbox + sending aliases) and the default
// "Reply From" (the primary -- i.e. the inbox that received the customer email).
export function useSupportEmails(orgId, brandId) {
  const [emails, setEmails] = useState([]);
  useEffect(() => {
    if (!brandId) { setEmails([]); return; }
    api.get("/support-emails/", { organization: orgId, brand: brandId })
      .then((d) => setEmails((d.results || d).filter((r) => r.is_active)))
      .catch(() => setEmails([]));
  }, [orgId, brandId]);
  const primary = emails.find((r) => r.is_primary) || emails[0];
  return { emails, defaultEmail: primary?.email || "" };
}
