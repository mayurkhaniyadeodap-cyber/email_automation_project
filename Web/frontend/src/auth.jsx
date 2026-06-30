import { createContext, useContext, useEffect, useState } from "react";
import { getToken, guestLogin, login as apiLogin, logoutApi, me, setToken } from "./api";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  async function refresh() {
    if (!getToken()) {
      // No token -> try auto-login (opens the panel without a sign-in screen).
      try {
        setUser(await guestLogin());
      } catch {
        setUser(null); // auto-login disabled -> fall back to the login page
      } finally {
        setLoading(false);
      }
      return;
    }
    try {
      setUser(await me());
    } catch {
      setToken("");
      setUser(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function login(username, password) {
    await apiLogin(username, password);
    setLoading(true);
    await refresh();
  }

  async function logout() {
    await logoutApi();   // records USER_LOGOUT + invalidates the token server-side
    setToken("");
    setUser(null);
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
