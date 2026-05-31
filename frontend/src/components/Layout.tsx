import { Link, NavLink, Outlet } from "react-router-dom";
import { UserButton } from "@clerk/clerk-react";

export function Layout() {
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
    </div>
  );
}
