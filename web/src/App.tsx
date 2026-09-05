/**
 * App — shadcn-style layout shell with collapsible grouped sidebar.
 */
import React, { useState, useEffect } from "react";
import { Routes, Route, NavLink, Navigate, useLocation, useNavigate } from "react-router-dom";
import {
  MessageSquare, History, BarChart2, Cpu, Server,
  BookOpen, Puzzle, Clock, Users, Settings, ScrollText,
  ChevronDown, ChevronRight, Zap, Wrench, Home,
  PanelLeftClose, PanelLeftOpen, AppWindow, Sun, Moon,
} from "lucide-react";

import { ChatPage }      from "./pages/ChatPage";
import { SessionsPage }  from "./pages/SessionsPage";
import { ConfigPage }    from "./pages/ConfigPage";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { SchedulerPage }      from "./pages/SchedulerPage";
import { SkillsPage }    from "./pages/SkillsPage";
import { APP_NAME, APP_SUBTITLE, ORG_NAME } from "./lib/constants";
import { ModelsPage }    from "./pages/ModelsPage";
import { LogsPage }      from "./pages/LogsPage";
import { PluginsPage }   from "./pages/PluginsPage";
import { ProfilesPage }  from "./pages/ProfilesPage";
import { McpPage }       from "./pages/McpPage";
import { AgentsPage }    from "./pages/AgentsPage";
import { ApplicationsManagePage } from "./pages/ApplicationsManagePage";
import { ApplicationDetailPage }  from "./pages/ApplicationDetailPage";
import { api } from "./lib/api";

// ── Nav config ────────────────────────────────────────────────────────────────

type NavItem  = { type: "item";  path: string; label: string; icon: React.ElementType };
type NavGroup = { type: "group"; label: string; icon: React.ElementType; items: NavItem[] };
type NavDef   = NavItem | NavGroup;

const NAV: NavDef[] = [
  { type: "item", path: "/chat",      label: "Chat",      icon: MessageSquare },
  { type: "item", path: "/sessions",  label: "Sessions",  icon: History },
  { type: "item", path: "/analytics", label: "Analytics", icon: BarChart2 },
  {
    type: "group", label: "AI Engine", icon: Zap,
    items: [
      { type: "item", path: "/models",  label: "Models",      icon: Cpu },
      { type: "item", path: "/mcp",     label: "MCP Servers", icon: Server },
      { type: "item", path: "/agents",  label: "Agents",      icon: Users },
      { type: "item", path: "/skills",  label: "Skills",      icon: BookOpen },
      { type: "item", path: "/plugins", label: "Plugins",     icon: Puzzle },
    ],
  },
  {
    type: "group", label: "Administration", icon: Wrench,
    items: [
      { type: "item", path: "/scheduler",     label: "Scheduler",    icon: Clock },
      { type: "item", path: "/profiles", label: "Profiles",     icon: Users },
      { type: "item", path: "/config",   label: "Configurations", icon: Settings },
      { type: "item", path: "/logs",     label: "Logs",         icon: ScrollText },
    ],
  },
];

const PATH_LABELS: Record<string, string> = {
  chat: "Chat", sessions: "Sessions", analytics: "Analytics",
  models: "Models",   mcp: "MCP Servers", agents: "Agents", skills: "Skills",
  plugins: "Plugins", scheduler: "Scheduler", profiles: "Profiles",
  config: "Configurations", logs: "Logs",
  applications: "Applications", manage: "Manage",
};

const SIDEBAR_W  = 240;
const SIDEBAR_CW = 56;  // collapsed width

// ── Styles ────────────────────────────────────────────────────────────────────

const S = {
  shell: { display:"flex", height:"100vh", background:"var(--bg)", overflow:"hidden" } as React.CSSProperties,

  sidebar: (collapsed: boolean): React.CSSProperties => ({
    width: collapsed ? SIDEBAR_CW : SIDEBAR_W,
    flexShrink: 0,
    background: "var(--sidebar-bg)",
    borderRight: "1px solid var(--border)",
    display: "flex", flexDirection: "column",
    overflow: "hidden",
    transition: "width 0.2s ease",
  }),

  logo: (collapsed: boolean): React.CSSProperties => ({
    height: "52px", padding: collapsed ? "0 14px" : "0 16px",
    display: "flex", alignItems: "center",
    justifyContent: collapsed ? "center" : "space-between",
    gap: "10px",
    borderBottom: "1px solid var(--border)", flexShrink: 0,
  }),
  logoLeft: { display:"flex", alignItems:"center", gap:"10px" } as React.CSSProperties,
  logoIcon: {
    width: "28px", height: "28px", borderRadius: "7px",
    background: "#456DE6",
    display: "flex", alignItems: "center", justifyContent: "center",
    flexShrink: 0,
  } as React.CSSProperties,
  logoText: { fontWeight:700, fontSize:"14px", color:"var(--fg)", letterSpacing:"-0.01em", whiteSpace:"nowrap" as const },
  logoSub:  { fontSize:"11px", color:"var(--fg-muted)", lineHeight:1.3 },

  toggleBtn: {
    background: "transparent", border: "none", cursor: "pointer",
    color: "var(--fg-muted)", padding: "4px", borderRadius: "4px",
    display: "flex", alignItems: "center", justifyContent: "center",
    flexShrink: 0,
    transition: "color 0.1s, background 0.1s",
  } as React.CSSProperties,

  navScroll: { flex:1, overflowY:"auto" as const, padding:"8px 0" },

  groupLabel: (collapsed: boolean): React.CSSProperties => ({
    display: collapsed ? "none" : "flex",
    alignItems: "center", justifyContent: "space-between",
    padding: "6px 16px", marginTop: "4px",
    fontSize: "11px", fontWeight: 600, textTransform: "uppercase",
    letterSpacing: "0.08em", color: "var(--fg-muted)",
    cursor: "pointer", userSelect: "none",
    transition: "color 0.1s",
  }),

  // collapsed group separator — tiny icon only
  groupSep: {
    height: "1px", background: "var(--border)",
    margin: "6px 12px",
  } as React.CSSProperties,

  navItem: (active: boolean, collapsed: boolean): React.CSSProperties => ({
    display: "flex", alignItems: "center",
    gap: collapsed ? 0 : "10px",
    padding: collapsed ? "8px 0" : "7px 16px",
    margin: collapsed ? "1px 6px" : "1px 8px",
    justifyContent: collapsed ? "center" : "flex-start",
    borderRadius: "6px",
    fontSize: "13.5px", fontWeight: active ? 500 : 400,
    color: active ? "var(--fg)" : "var(--fg-muted)",
    background: active ? "var(--sidebar-active)" : "transparent",
    textDecoration: "none",
    cursor: "pointer",
    transition: "background 0.1s, color 0.1s",
    borderLeft: (!collapsed && active) ? "2px solid var(--fg-muted)" : "2px solid transparent",
    outline: (collapsed && active) ? "2px solid var(--fg-muted)" : "none",
    outlineOffset: collapsed ? "-2px" : "0",
    position: "relative" as const,
  }),

  userCard: (collapsed: boolean): React.CSSProperties => ({
    padding: collapsed ? "12px 0" : "12px 16px",
    borderTop: "1px solid var(--border)",
    display: "flex", alignItems: "center",
    justifyContent: collapsed ? "center" : "flex-start",
    gap: "10px", flexShrink: 0,
  }),
  avatar: {
    width: "32px", height: "32px", borderRadius: "50%",
    background: "#456DE6",
    display: "flex", alignItems: "center", justifyContent: "center",
    fontSize: "12px", fontWeight: 700, color: "#fff", flexShrink: 0,
  } as React.CSSProperties,
  userName: { fontSize:"13px", fontWeight:600, color:"var(--fg)", lineHeight:1.3, whiteSpace:"nowrap" as const },
  userOrg:  { fontSize:"11px", color:"var(--fg-muted)", lineHeight:1.3, whiteSpace:"nowrap" as const },

  main:    { flex:1, display:"flex", flexDirection:"column" as const, overflow:"hidden" },

  topbar: {
    height: "52px", flexShrink: 0,
    borderBottom: "1px solid var(--border)",
    display: "flex", alignItems: "center", padding: "0 24px",
    background: "var(--sidebar-bg)", gap: "6px",
  } as React.CSSProperties,
  crumb:    { fontSize:"13px", color:"var(--fg-muted)" },
  crumbSep: { fontSize:"13px", color:"var(--fg-subtle)", margin:"0 2px" },
  crumbCur: { fontSize:"13px", fontWeight:500, color:"var(--fg)" },

  content: { flex:1, overflow:"hidden", display:"flex", flexDirection:"column" as const } as React.CSSProperties,

  // tooltip shown when collapsed
  tooltip: {
    position: "absolute" as const,
    left: "calc(100% + 10px)",
    top: "50%", transform: "translateY(-50%)",
    background: "#1e1e23",
    border: "1px solid var(--border)",
    color: "var(--fg)",
    fontSize: "12px", fontWeight: 500,
    padding: "4px 8px", borderRadius: "5px",
    whiteSpace: "nowrap" as const,
    pointerEvents: "none" as const,
    zIndex: 999,
    boxShadow: "0 4px 12px rgba(0,0,0,0.4)",
  },
};

// ── Components ────────────────────────────────────────────────────────────────

function NavItemLink({ item, collapsed }: { item: NavItem; collapsed: boolean }) {
  const [hovered, setHovered] = useState(false);

  return (
    <NavLink
      to={item.path}
      title={collapsed ? item.label : undefined}
      style={({ isActive }) => ({
        ...S.navItem(isActive, collapsed),
        background: hovered && !isActive
          ? "var(--sidebar-hover)"
          : isActive ? "var(--sidebar-active)" : "transparent",
      })}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <item.icon size={15} strokeWidth={1.75} style={{ flexShrink: 0 }} />
      {!collapsed && item.label}
      {/* Tooltip when collapsed + hovered */}
      {collapsed && hovered && (
        <span style={S.tooltip}>{item.label}</span>
      )}
    </NavLink>
  );
}

function NavGroupSection({ group, collapsed }: { group: NavGroup; collapsed: boolean }) {
  const location = useLocation();
  const anyActive = group.items.some(i => location.pathname.startsWith(i.path));
  const [open, setOpen] = useState(true);

  return (
    <div>
      {collapsed
        ? <div style={S.groupSep} />
        : (
          <div
            style={{ ...S.groupLabel(collapsed), color: anyActive ? "var(--fg)" : "var(--fg-muted)" }}
            onClick={() => setOpen(o => !o)}
          >
            <span style={{ display:"flex", alignItems:"center", gap:"6px" }}>
              <group.icon size={12} strokeWidth={2} />
              {group.label}
            </span>
            {open ? <ChevronDown size={12} strokeWidth={2} /> : <ChevronRight size={12} strokeWidth={2} />}
          </div>
        )
      }
      {(open || collapsed) && group.items.map(item => (
        <NavItemLink key={item.path} item={item} collapsed={collapsed} />
      ))}
    </div>
  );
}

function Breadcrumb() {
  const location = useLocation();
  const parts = location.pathname.split("/").filter(Boolean);
  return (
    <div style={{ display:"flex", alignItems:"center" }}>
      <span style={S.crumb}>
        <Home size={13} style={{ display:"inline", marginRight:"4px", verticalAlign:"middle" }} />
        Home
      </span>
      {parts.map((part, i) => (
        <React.Fragment key={i}>
          <span style={S.crumbSep}> / </span>
          <span style={i === parts.length - 1 ? S.crumbCur : S.crumb}>
            {PATH_LABELS[part] ?? part.charAt(0).toUpperCase() + part.slice(1)}
          </span>
        </React.Fragment>
      ))}
    </div>
  );
}

// ── Applications nav section (dynamic, with gear + enabled-app sub-items) ────

interface AppEntry { id: string; name: string }

function NavApplicationsSection({ collapsed }: { collapsed: boolean }) {
  const location  = useLocation();
  const navigate  = useNavigate();
  const [apps, setApps]       = useState<AppEntry[]>([]);
  const [open, setOpen]       = useState(true);
  const [hovered, setHovered] = useState(false);

  useEffect(() => {
    api.getApplications()
      .then(res => setApps((res.applications || []).filter((a: any) => a.enabled)))
      .catch(() => {});
  }, [location.pathname]);   // refresh list when navigation changes

  const isManageActive = location.pathname === "/applications/manage";
  const anyActive      = location.pathname.startsWith("/applications");

  if (collapsed) {
    return (
      <div style={{ position: "relative" }}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
      >
        <div
          onClick={() => navigate("/applications/manage")}
          style={{
            ...S.navItem(anyActive, true),
            background: hovered && !anyActive
              ? "var(--sidebar-hover)"
              : anyActive ? "var(--sidebar-active)" : "transparent",
          }}
        >
          <AppWindow size={15} strokeWidth={1.75} style={{ flexShrink: 0 }} />
          {hovered && <span style={S.tooltip}>Applications</span>}
        </div>
      </div>
    );
  }

  return (
    <div>
      {/* Section header row */}
      <div
        style={{
          display: "flex", alignItems: "center",
          padding: "6px 16px",
          margin: "4px 0 1px 0",
          fontSize: "11px", fontWeight: 600, textTransform: "uppercase",
          letterSpacing: "0.08em",
          color: anyActive ? "var(--fg)" : "var(--fg-muted)",
          cursor: "pointer", userSelect: "none",
        }}
        onClick={() => setOpen(o => !o)}
      >
        <AppWindow size={12} strokeWidth={2} style={{ marginRight: "6px", flexShrink: 0 }} />
        <span style={{ flex: 1 }}>Applications</span>
        {/* Gear → manage page */}
        <span
          title="Manage Applications"
          onClick={e => { e.stopPropagation(); navigate("/applications/manage"); }}
          style={{
            display: "flex", alignItems: "center",
            color: isManageActive ? "var(--fg)" : "var(--fg-muted)",
            padding: "2px 4px", borderRadius: "3px",
            transition: "color 0.1s",
          }}
        >
          <Settings size={12} strokeWidth={2} />
        </span>
        <span style={{ marginLeft: "4px" }}>
          {open ? <ChevronDown size={12} strokeWidth={2} /> : <ChevronRight size={12} strokeWidth={2} />}
        </span>
      </div>

      {/* Enabled app sub-items */}
      {open && apps.map(app => (
        <NavLink
          key={app.id}
          to={`/applications/${app.id}`}
          style={({ isActive }) => ({
            ...S.navItem(isActive, false),
            paddingLeft: "30px",
          })}
        >
          <AppWindow size={13} strokeWidth={1.75} style={{ flexShrink: 0 }} />
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {app.name}
          </span>
        </NavLink>
      ))}

      {open && apps.length === 0 && (
        <div style={{
          padding: "4px 30px 6px",
          fontSize: "12px", color: "var(--fg-subtle)",
          fontStyle: "italic",
        }}>
          No enabled apps
        </div>
      )}
    </div>
  );
}

/** Wraps non-chat pages so they scroll independently without affecting the outer shell. */
function ScrollPage({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ flex: 1, overflow: "auto", height: "100%" }}>
      {children}
    </div>
  );
}

// ── App ───────────────────────────────────────────────────────────────────────

export function App() {
  const [collapsed, setCollapsed] = useState(false);

  // ── Theme toggle ──────────────────────────────────────────────────────────
  const [isDark, setIsDark] = useState<boolean>(() => {
    const stored = localStorage.getItem("gyrfalcon-theme");
    if (stored) return stored === "dark";
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  });

  useEffect(() => {
    const root = document.documentElement;
    if (isDark) {
      root.setAttribute("data-theme", "dark");
    } else {
      root.setAttribute("data-theme", "light");
    }
    localStorage.setItem("gyrfalcon-theme", isDark ? "dark" : "light");
  }, [isDark]);

  return (
    <div style={S.shell}>
      {/* ── Sidebar ── */}
      <aside style={S.sidebar(collapsed)}>
        {/* Logo + toggle */}
        <div style={S.logo(collapsed)}>
          {!collapsed && (
            <div style={S.logoLeft}>
              <div style={S.logoIcon}>
                <span style={{ color:"#fff", fontWeight:900, fontSize:"13px" }}>G</span>
              </div>
              <div>
                <div style={S.logoText}>{APP_NAME}</div>
                <div style={S.logoSub}>{APP_SUBTITLE}</div>
              </div>
            </div>
          )}
          {collapsed && (
            <div style={S.logoIcon}>
              <span style={{ color:"#fff", fontWeight:900, fontSize:"13px" }}>G</span>
            </div>
          )}
          {!collapsed && (
            <button
              style={S.toggleBtn}
              onClick={() => setCollapsed(true)}
              title="Collapse sidebar"
            >
              <PanelLeftClose size={16} />
            </button>
          )}
        </div>

        {/* Navigation */}
        <div style={S.navScroll}>
          {NAV.slice(0, 3).map((def) =>
            def.type === "item"
              ? <NavItemLink key={def.path} item={def} collapsed={collapsed} />
              : <NavGroupSection key={def.label} group={def} collapsed={collapsed} />
          )}
          <NavApplicationsSection collapsed={collapsed} />
          {NAV.slice(3).map((def) =>
            def.type === "item"
              ? <NavItemLink key={def.path} item={def} collapsed={collapsed} />
              : <NavGroupSection key={def.label} group={def} collapsed={collapsed} />
          )}
        </div>

        {/* Expand button (collapsed mode only) */}
        {collapsed && (
          <div style={{ padding:"8px 0", display:"flex", justifyContent:"center", borderTop:"1px solid var(--border)" }}>
            <button
              style={{ ...S.toggleBtn, padding:"6px" }}
              onClick={() => setCollapsed(false)}
              title="Expand sidebar"
            >
              <PanelLeftOpen size={16} />
            </button>
          </div>
        )}

        {/* User + theme toggle */}
        <div style={S.userCard(collapsed)}>
          <div style={S.avatar} title={collapsed ? `Zhang, Alex · ${ORG_NAME}` : undefined}>
            ZA
          </div>
          {!collapsed && (
            <>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={S.userName}>Zhang, Alex</div>
                <div style={S.userOrg}>{ORG_NAME}</div>
              </div>
              <button
                onClick={() => setIsDark(d => !d)}
                title={isDark ? "Switch to light theme" : "Switch to dark theme"}
                style={{
                  background: "transparent",
                  border: "1px solid var(--border)",
                  borderRadius: "6px",
                  padding: "5px 6px",
                  cursor: "pointer",
                  color: "var(--fg-muted)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  flexShrink: 0,
                  transition: "color 0.15s, border-color 0.15s",
                }}
              >
                {isDark ? <Sun size={14} /> : <Moon size={14} />}
              </button>
            </>
          )}
          {collapsed && (
            <button
              onClick={() => setIsDark(d => !d)}
              title={isDark ? "Switch to light theme" : "Switch to dark theme"}
              style={{
                background: "transparent", border: "none",
                cursor: "pointer", color: "var(--fg-muted)",
                display: "flex", alignItems: "center", padding: "4px",
              }}
            >
              {isDark ? <Sun size={13} /> : <Moon size={13} />}
            </button>
          )}
        </div>
      </aside>

      {/* ── Main ── */}
      <div style={S.main}>
        <header style={S.topbar}>
          <Breadcrumb />
        </header>
        <div style={S.content}>
          <Routes>
            <Route path="/"          element={<Navigate to="/chat"      replace />} />
            <Route path="/chat"      element={<ChatPage />} />
            <Route path="/sessions"  element={<ScrollPage><SessionsPage /></ScrollPage>} />
            <Route path="/analytics" element={<ScrollPage><AnalyticsPage /></ScrollPage>} />
            <Route path="/models"    element={<ScrollPage><ModelsPage /></ScrollPage>} />
            <Route path="/mcp"       element={<McpPage />} />
            <Route path="/agents"    element={<AgentsPage />} />
            <Route path="/config"    element={<ScrollPage><ConfigPage /></ScrollPage>} />
            <Route path="/scheduler"      element={<SchedulerPage />} />
            <Route path="/skills"    element={<SkillsPage />} />
            <Route path="/plugins"   element={<ScrollPage><PluginsPage /></ScrollPage>} />
            <Route path="/profiles"  element={<ScrollPage><ProfilesPage /></ScrollPage>} />
            <Route path="/logs"      element={<ScrollPage><LogsPage /></ScrollPage>} />
            <Route path="/applications/manage"  element={<ApplicationsManagePage />} />
            <Route path="/applications/:id"     element={<ApplicationDetailPage />} />
            <Route path="/applications"         element={<Navigate to="/applications/manage" replace />} />
          </Routes>
        </div>
      </div>
    </div>
  );
}

