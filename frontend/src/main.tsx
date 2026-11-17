import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes } from "react-router-dom";
import { getToken, setToken } from "./api/client";
import { AdminPage } from "./features/admin/AdminPage";
import { AnalyticsPage } from "./features/analytics/AnalyticsPage";
import { LoginPage } from "./features/auth/LoginPage";
import { DashboardPage } from "./features/dashboard/DashboardPage";
import { OperatorPage } from "./features/operator/OperatorPage";
import "./index.css";
import { t } from "./shared/i18n";
import { roleLabel, useMe } from "./shared/session";

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } });

function Shell() {
  const { data: me } = useMe();
  const link = ({ isActive }: { isActive: boolean }) =>
    `rounded px-3 py-2 text-sm font-medium ${isActive ? "bg-slate-700 text-white" : "text-slate-300 hover:bg-slate-800"}`;
  const menu = me?.menu;
  const admin = menu && (menu.references || menu.structure || menu.users);
  return (
    <div className="min-h-full">
      <header className="flex flex-wrap items-center gap-2 border-b border-slate-800 bg-slate-900 px-4 py-2">
        <span className="mr-4 text-base font-semibold">{t.app.title}</span>
        {menu?.dashboard && (
          <NavLink to="/dashboard" className={link}>
            {t.nav.dashboard}
          </NavLink>
        )}
        {menu?.analytics && (
          <NavLink to="/analytics" className={link}>
            {t.nav.analytics}
          </NavLink>
        )}
        {admin && (
          <NavLink to="/admin" className={link}>
            {t.nav.admin}
          </NavLink>
        )}
        <span className="ml-auto text-sm text-slate-400">
          {me ? `${me.name} · ${me.superadmin ? t.users.superadmin : me.roles.map(roleLabel).join(", ")}` : ""}
        </span>
        <button
          className="rounded px-3 py-2 text-sm text-slate-400 hover:bg-slate-800"
          onClick={() => {
            setToken(null);
            queryClient.clear();
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

/** Start page by kind of access: a kiosk opens its machine, a TV its area, a person the first allowed section. */
function Home() {
  const { data: me, isLoading } = useMe();
  if (!getToken()) return <Navigate to="/login" replace />;
  if (isLoading || !me) return <p className="p-8 text-slate-400">{t.common.loading}</p>;
  if (me.kiosk?.machine_id) return <Navigate to={`/operator/${me.kiosk.machine_id}`} replace />;
  if (me.kiosk?.area_id) return <Navigate to={`/dashboard/${me.kiosk.area_id}`} replace />;
  if (me.menu.dashboard) return <Navigate to="/dashboard" replace />;
  if (me.menu.analytics) return <Navigate to="/analytics" replace />;
  return <Navigate to="/admin" replace />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/" element={<Home />} />
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/dashboard/:areaId" element={<DashboardPage />} />
          <Route path="/operator/:machineId" element={<OperatorPage />} />
          <Route element={<Shell />}>
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/admin" element={<AdminPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
