import { Navigate, Route, Routes } from "react-router-dom";
import { SignedIn, SignedOut, SignInButton } from "@clerk/clerk-react";
import { Layout } from "./components/Layout";
import { LeaderboardPage } from "./pages/LeaderboardPage";
import { EditorPage } from "./pages/EditorPage";

function SignInGate() {
  return (
    <div className="gate">
      <div className="brand">
        <span className="mark">▰</span>
        <span>Snake Arena</span>
      </div>
      <p style={{ color: "var(--text-dim)" }}>Sign in with GitHub to build and battle your agents.</p>
      <SignInButton mode="modal">
        <button className="btn primary">Sign in</button>
      </SignInButton>
    </div>
  );
}

export function App() {
  return (
    <>
      <SignedOut>
        <SignInGate />
      </SignedOut>
      <SignedIn>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Navigate to="/leaderboard" replace />} />
            <Route path="/leaderboard" element={<LeaderboardPage />} />
            <Route path="/editor" element={<EditorPage />} />
            <Route path="*" element={<Navigate to="/leaderboard" replace />} />
          </Route>
        </Routes>
      </SignedIn>
    </>
  );
}
