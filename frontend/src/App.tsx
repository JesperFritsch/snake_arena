import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { LeaderboardPage } from "./pages/LeaderboardPage";
import { EditorPage } from "./pages/EditorPage";
import { LegalPage } from "./pages/LegalPage";
import termsMd from "../../docs/legal/terms_of_service.md?raw";
import privacyMd from "../../docs/legal/privacy_policy.md?raw";
import aupMd from "../../docs/legal/acceptable_use_policy.md?raw";

function AppRoutes() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/leaderboard" replace />} />
        <Route path="/leaderboard" element={<LeaderboardPage />} />
        <Route path="/editor" element={<EditorPage />} />
        <Route path="*" element={<Navigate to="/leaderboard" replace />} />
      </Route>
    </Routes>
  );
}

export function App() {
  return (
    <Routes>
      <Route path="/terms" element={<LegalPage title="Terms of Service" doc={termsMd} />} />
      <Route path="/privacy" element={<LegalPage title="Privacy Policy" doc={privacyMd} />} />
      <Route path="/acceptable-use" element={<LegalPage title="Acceptable Use Policy" doc={aupMd} />} />
      <Route path="*" element={<AppRoutes />} />
    </Routes>
  );
}
