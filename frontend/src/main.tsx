import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes } from "react-router-dom";
import { setToken } from "./api/client";
import { AdminPage } from "./features/admin/AdminPage";
import { AnalyticsPage } from "./features/analytics/AnalyticsPage";
import { LoginPage } from "./features/auth/LoginPage";
import { DashboardPage } from "./features/dashboard/DashboardPage";
import { OperatorPage } from "./features/operator/OperatorPage";
import "./index.css";
import { t } from "./shared/i18n";

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } });

function Shell() {
  const link = ({ isActive }: { isActive: boolean }) =>
    `rounded px-3 py-2 text-sm font-medium ${isActive ? "bg-slate-700 text-white" : "text-slate-300 hover:bg-slate-800"}`;
  return (
    <div className="min-h-full">
      <header className="flex items-center gap-2 border-b border-slate-800 bg-slate-900 px-4 py-2">
        <span className="mr-4 text-base font-semibold">{t.app.title}</span>
        <NavLink to="/dashboard" className={link}>
          {t.nav.dashboard}
        </NavLink>
        <NavLink to="/analytics" className={link}>
          {t.nav.analytics}
        </NavLink>
        <NavLink to="/admin" className={link}>
          {t.nav.admin}
        </NavLink>
        <button
          className="ml-auto rounded px-3 py-2 text-sm text-slate-400 hover:bg-slate-800"
          onClick={() => {
            setToken(null);
            window.location.assign("/login");
          }}
        >
          {t.nav.logout}
        </button>
      </header>
      <main className="p-4">
        <Outlet />
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/dashboard/:areaId" element={<DashboardPage />} />
          <Route path="/operator/:machineId" element={<OperatorPage />} />
          <Route element={<Shell />}>
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/admin" element={<AdminPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
