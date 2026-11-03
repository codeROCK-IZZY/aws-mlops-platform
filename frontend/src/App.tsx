import type { ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import type { Role } from "./api/types";
import { Layout } from "./components/Layout";
import { Loading } from "./components/ui";
import { useAuth } from "./lib/auth";
import { AdminPage } from "./pages/AdminPage";
import { DatasetsPage } from "./pages/DatasetsPage";
import { LoginPage } from "./pages/LoginPage";
import { ModelsPage } from "./pages/ModelsPage";
import { MonitoringPage } from "./pages/MonitoringPage";
import { OverviewPage } from "./pages/OverviewPage";
import { RunDetailPage, RunsPage } from "./pages/RunsPage";

function RequireRole({ role, children }: { role: Role; children: ReactNode }) {
  const { can } = useAuth();
  return can(role) ? <>{children}</> : <p className="p-6 text-sm text-slate-600">You don't have access to this page.</p>;
}

export function App() {
  const { user, loading } = useAuth();
  if (loading) return <Loading />;
  if (!user) return <Routes><Route path="*" element={<LoginPage />} /></Routes>;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<OverviewPage />} />
        <Route path="runs" element={<RunsPage />} />
        <Route path="runs/:id" element={<RunDetailPage />} />
        <Route path="models" element={<ModelsPage />} />
        <Route path="monitoring" element={<MonitoringPage />} />
        <Route path="datasets" element={<DatasetsPage />} />
        <Route path="admin" element={<RequireRole role="admin"><AdminPage /></RequireRole>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
