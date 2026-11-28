import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { setToken } from "../api/client";
import { t } from "./i18n";
import { NodePicker, roleLabel, useMe, useNodeSelection } from "./session";

type Selection = ReturnType<typeof useNodeSelection>;
const NodeContext = createContext<Selection | null>(null);

/** Site and area chosen at the top of the screen; every page inside the shell works on them. */
export function useShellNode(): Selection {
  const selection = useContext(NodeContext);
  if (!selection) throw new Error("useShellNode outside of Shell");
  return selection;
}

// Simple line icons (24 × 24, stroke = currentColor)
const ICONS: Record<string, string> = {
  overview: "M3 13h8V3H3zM13 21h8v-8h-8zM3 21h8v-6H3zM13 3v8h8V3z",
  machines: "M4 6h16v10H4zM8 20h8M12 16v4",
  production: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  alarms: "M12 3 2 20h20zM12 10v4M12 17v.01",
  repairs: "M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z",
  service: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z",
  admin: "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
  profile: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
  menu: "M3 6h18M3 12h18M3 18h18",
  collapse: "M15 18l-6-6 6-6",
  expand: "M9 18l6-6-6-6",
};

export function Icon({ name, className = "h-5 w-5" }: { name: string; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d={ICONS[name]} />
    </svg>
  );
}

const COLLAPSED_KEY = "mm_menu_collapsed";

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function Item({ to, icon, label, collapsed, end }: { to: string; icon: string; label: string; collapsed: boolean; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      title={collapsed ? label : undefined}
      className={({ isActive }) =>
        `flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium ${
          isActive ? "bg-sky-600/20 text-sky-300" : "text-slate-300 hover:bg-slate-800 hover:text-white"
        } ${collapsed ? "justify-center" : ""}`
      }
    >
      <Icon name={icon} />
      {!collapsed && <span className="truncate">{label}</span>}
    </NavLink>
  );
}

/** Application frame: menu on the left (icons only when collapsed, behind a button below 768 px), site and area on top. */
export function Shell({ children }: { children?: ReactNode }) {
  const { data: me } = useMe();
  const queryClient = useQueryClient();
  const selection = useNodeSelection(me?.sites, { requireSite: true });
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [open, setOpen] = useState(false); // the drawer on a phone
  const location = useLocation();

  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    try {
      localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
    } catch {
      /* not remembered */
    }
  }, [collapsed]);

  const menu = me?.menu;
  const admin = menu && (menu.references || menu.structure || menu.users);
  const items = [
    menu?.analytics && { to: "/", icon: "overview", label: t.nav.overview, end: true },
    menu?.dashboard && { to: "/machines", icon: "machines", label: t.nav.machines, end: true },
    menu?.analytics && { to: "/analytics", icon: "production", label: t.nav.production },
    menu?.alarms && { to: "/alarms", icon: "alarms", label: t.nav.alarms },
    menu?.repairs && { to: "/repairs", icon: "repairs", label: t.nav.repairs },
    menu?.service && { to: "/service", icon: "service", label: t.nav.service },
    admin && { to: "/admin", icon: "admin", label: t.nav.admin },
  ].filter(Boolean) as { to: string; icon: string; label: string; end?: boolean }[];

  const logout = () => {
    setToken(null);
    queryClient.clear();
    window.location.assign("/login");
  };

  // on a phone the drawer always shows labels
  const sidebar = (narrow: boolean) => (
    <div className="flex h-full flex-col gap-1 p-2">
      <div className={`mb-2 flex items-center gap-2 px-2 py-2 ${narrow ? "justify-center" : ""}`}>
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-sky-600 text-sm font-bold text-white">
          MM
        </span>
        {!narrow && <span className="truncate text-sm font-semibold">{t.app.title}</span>}
      </div>
      <nav className="flex flex-1 flex-col gap-1">
        {items.map((item) => (
          <Item key={item.to} {...item} collapsed={narrow} />
        ))}
      </nav>
      <div className="flex flex-col gap-1 border-t border-slate-800 pt-2">
        <NavLink
          to="/profile"
          title={narrow ? t.nav.profile : undefined}
          className={({ isActive }) =>
            `flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm ${isActive ? "bg-sky-600/20 text-sky-300" : "text-slate-300 hover:bg-slate-800"} ${narrow ? "justify-center" : ""}`
          }
        >
          <Icon name="profile" />
          {!narrow && (
            <span className="min-w-0">
              <span className="block truncate font-medium">{me?.name}</span>
              <span className="block truncate text-xs text-slate-500">
                {me ? (me.superadmin ? t.users.superadmin : me.roles.map(roleLabel).join(", ")) : ""}
              </span>
            </span>
          )}
        </NavLink>
        <button
          onClick={logout}
          title={narrow ? t.nav.logout : undefined}
          className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm text-slate-400 hover:bg-slate-800 ${narrow ? "justify-center" : ""}`}
        >
          <Icon name="logout" />
          {!narrow && t.nav.logout}
        </button>
      </div>
    </div>
  );

  return (
    <NodeContext.Provider value={selection}>
      <div className="flex min-h-full">
        {/* desktop: fixed column, collapsible to icons */}
        <aside
          className={`sticky top-0 hidden h-screen shrink-0 border-r border-slate-800 bg-slate-900 md:block ${collapsed ? "w-16" : "w-60"}`}
        >
          {sidebar(collapsed)}
        </aside>
        {/* phone: drawer behind the menu button */}
        {open && (
          <div className="fixed inset-0 z-40 md:hidden">
            <div className="absolute inset-0 bg-black/60" onClick={() => setOpen(false)} />
            <aside className="absolute inset-y-0 left-0 w-64 border-r border-slate-800 bg-slate-900">{sidebar(false)}</aside>
          </div>
        )}
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-30 flex flex-wrap items-center gap-2 border-b border-slate-800 bg-slate-950/95 px-3 py-2 backdrop-blur">
            <button
              className="rounded-lg p-2 text-slate-300 hover:bg-slate-800 md:hidden"
              onClick={() => setOpen(true)}
              aria-label={t.nav.menu}
            >
              <Icon name="menu" />
            </button>
            <button
              className="hidden rounded-lg p-2 text-slate-400 hover:bg-slate-800 md:block"
              onClick={() => setCollapsed((c) => !c)}
              aria-label={collapsed ? t.nav.expand : t.nav.collapse}
              title={collapsed ? t.nav.expand : t.nav.collapse}
            >
              <Icon name={collapsed ? "expand" : "collapse"} />
            </button>
            {me && <NodePicker sites={me.sites} selection={selection} allowAllSites={false} />}
            {me && me.sites.length === 1 && (
              <span className="text-sm font-medium text-slate-300">{selection.site?.name}</span>
            )}
          </header>
          <main className="min-w-0 flex-1 p-3 md:p-4">{children ?? <Outlet />}</main>
        </div>
      </div>
    </NodeContext.Provider>
  );
}
