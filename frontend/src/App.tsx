import { Link, Navigate, Route, Routes } from "react-router-dom";
import { SignedIn, SignedOut, SignInButton } from "@clerk/clerk-react";
import { Layout } from "./components/Layout";
import { LeaderboardPage } from "./pages/LeaderboardPage";
import { EditorPage } from "./pages/EditorPage";
import { LegalPage } from "./pages/LegalPage";
import termsMd from "../../docs/legal/terms_of_service.md?raw";
import privacyMd from "../../docs/legal/privacy_policy.md?raw";
import aupMd from "../../docs/legal/acceptable_use_policy.md?raw";

function SignInGate() {
  return (
    <div className="gate">
      <div className="brand">
        <span className="mark">▰</span>
        <span>Gridsnake</span>
      </div>
      <p style={{ color: "var(--text-dim)" }}>Sign in with GitHub to build and battle your agents.</p>
      <SignInButton mode="modal">
        <button className="btn primary">Sign in</button>
      </SignInButton>
      <p className="gate-consent">
        By signing in you agree to the{" "}
        <Link to="/terms">Terms of Service</Link> and{" "}
        <Link to="/privacy">Privacy Policy</Link>.
      </p>
    </div>
  );
}

function GatedRoutes() {
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

export function App() {
  return (
    <Routes>
      <Route path="/terms" element={<LegalPage title="Terms of Service" doc={termsMd} />} />
      <Route path="/privacy" element={<LegalPage title="Privacy Policy" doc={privacyMd} />} />
      <Route path="/acceptable-use" element={<LegalPage title="Acceptable Use Policy" doc={aupMd} />} />
      <Route path="*" element={<GatedRoutes />} />
    </Routes>
  );
}
