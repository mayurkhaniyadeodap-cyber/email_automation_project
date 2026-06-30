import { createContext, useContext, useEffect, useState } from "react";
import { api } from "./api";
import { useAuth } from "./auth";

// The panel's two top dropdowns (doc §9): select Organization -> select Brand.
// Everything below is scoped to the selected brand. Selection persists in localStorage.

const ScopeContext = createContext(null);

export function ScopeProvider({ children }) {
  const { user } = useAuth();
  const [orgId, setOrgId] = useState(localStorage.getItem("scope_org") || "");
  const [brandId, setBrandId] = useState(localStorage.getItem("scope_brand") || "");
  const [brands, setBrands] = useState([]);

  const orgs = user?.organizations || [];

  // Default the org once we know the user's orgs.
  useEffect(() => {
    if (!orgId && orgs.length) selectOrg(String(orgs[0].id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  // Load brands whenever the org changes.
  useEffect(() => {
    if (!orgId) {
      setBrands([]);
      return;
    }
    api.get("/brands/", { organization: orgId }).then((data) => {
      const list = data.results || data;
      setBrands(list);
      if (!list.find((b) => String(b.id) === brandId)) {
        selectBrand(list.length ? String(list[0].id) : "");
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId]);

  function selectOrg(id) {
    setOrgId(id);
    localStorage.setItem("scope_org", id);
  }
  function selectBrand(id) {
    setBrandId(id);
    localStorage.setItem("scope_brand", id);
  }

  return (
    <ScopeContext.Provider
      value={{ orgId, brandId, orgs, brands, selectOrg, selectBrand }}
    >
      {children}
    </ScopeContext.Provider>
  );
}

export const useScope = () => useContext(ScopeContext);
