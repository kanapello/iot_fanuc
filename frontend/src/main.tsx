import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { getToken } from "./api/client";
import { AdminPage } from "./features/admin/AdminPage";
import { AlarmsPage } from "./features/alarms/AlarmsPage";
import { AnalyticsPage } from "./features/analytics/AnalyticsPage";
import { LoginPage } from "./features/auth/LoginPage";
import { DashboardPage, MachinesPage } from "./features/dashboard/DashboardPage";
import { MachineCardPage } from "./features/geometry/MachineCardPage";
import { OperatorPage } from "./features/operator/OperatorPage";
import { OverviewPage } from "./features/overview/OverviewPage";
import { PassportPage } from "./features/passport/PassportPage";
import { ProfilePage } from "./features/profile/ProfilePage";
import { RepairsPage } from "./features/repairs/RepairsPage";
import { ServicePage } from "./features/service/ServicePage";
import "./index.css";
import { t } from "./shared/i18n";
import { useMe } from "./shared/session";
import { Shell } from "./shared/Shell";

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } });

/** Start page by kind of access: a kiosk opens its machine, a TV its area, a person with analytics the
 * overview, anyone else the first allowed section (as before the overview). */
function Home() {
  const { data: me, isLoading } = useMe();
  if (!getToken()) return <Navigate to="/login" replace />;
  if (isLoading || !me) return <p className="p-8 text-slate-400">{t.common.loading}</p>;
  if (me.kiosk?.machine_id) return <Navigate to={`/operator/${me.kiosk.machine_id}`} replace />;
  if (me.kiosk?.area_id) return <Navigate to={`/dashboard/${me.kiosk.area_id}`} replace />;
  // people with analytics get the overview of the enterprise right here, inside the application frame
  if (me.menu.analytics)
    return (
      <Shell>
        <OverviewPage />
      </Shell>
    );
  if (me.menu.dashboard) return <Navigate to="/dashboard" replace />;
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
          <Route path="/m/:code" element={<PassportPage />} />
          <Route element={<Shell />}>
            <Route path="/machines" element={<MachinesPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/admin" element={<AdminPage />} />
            <Route path="/alarms" element={<AlarmsPage />} />
            <Route path="/repairs" element={<RepairsPage />} />
            <Route path="/service" element={<ServicePage />} />
            <Route path="/machines/:machineId" element={<MachineCardPage />} />
            <Route path="/profile" element={<ProfilePage />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
