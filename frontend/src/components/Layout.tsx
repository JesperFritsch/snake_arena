import { useState, useEffect } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { UserButton } from "@clerk/clerk-react";
import { GuideDialog } from "./GuideDialog";

const GUIDE_SEEN_KEY = "snake_arena_guide_seen";

export function Layout() {
  const [guideOpen, setGuideOpen] = useState(false);

  useEffect(() => {
    if (!localStorage.getItem(GUIDE_SEEN_KEY)) {
      setGuideOpen(true);
    }
  }, []);

  function closeGuide() {
    localStorage.setItem(GUIDE_SEEN_KEY, "1");
    setGuideOpen(false);
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="mark">▰</span>
          <span>Snake Arena</span>
          <span className="ital">battle your agents</span>
        </div>
        <nav className="nav">
          <NavLink to="/leaderboard" className={({ isActive }) => (isActive ? "active" : "")}>
            Leaderboard
          </NavLink>
          <NavLink to="/editor" className={({ isActive }) => (isActive ? "active" : "")}>
            Editor
          </NavLink>
          <button
            className="btn ghost"
            style={{ padding: "4px 11px", fontSize: 12, letterSpacing: "1px", textTransform: "uppercase" }}
            onClick={() => setGuideOpen(true)}
          >
            Guide
          </button>
        </nav>
        <span className="topbar-spacer" />
        <UserButton afterSignOutUrl="/" />
      </header>
      <main className="main">
        <Outlet />
      </main>
      <footer className="footer">
        <Link to="/terms">Terms</Link>
        <Link to="/privacy">Privacy</Link>
        <Link to="/acceptable-use">Acceptable Use</Link>
      </footer>

      {guideOpen && <GuideDialog onClose={closeGuide} />}
    </div>
  );
}
