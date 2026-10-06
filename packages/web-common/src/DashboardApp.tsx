/**
 * App — shadcn-style layout shell with collapsible grouped sidebar.
 */
import React, { useState, useEffect } from "react";
import { Routes, Route, NavLink, Navigate, useLocation } from "react-router-dom";
import {
  MessageSquare, History, BarChart2, Calculator, Cpu, Server,
  BookOpen, Puzzle, Clock, Users, Settings, ScrollText,
  ChevronDown, ChevronRight, Zap, Wrench, Home,
  PanelLeftClose, PanelLeftOpen, Sun, Moon,
  LogOut,
  Workflow, Play, Inbox, CalendarClock, Radio,
  ShieldCheck, KeyRound, Lock,
  UsersRound, PanelTop, ListTree,
} from "lucide-react";

import { APP_NAME, APP_SUBTITLE, ORG_NAME } from "./lib/constants";
import { api } from "./lib/api";
import { ChangePasswordPage, LoginPage } from "./auth/LoginPage";
import type { AppKey, NavigationPayload, PageRegistration, RegisteredPage } from "./pageRegistry";

// The sidebar and client routes are built from database menu/page records.
type NavItem = { type: "item"; path: string; label: string; icon: React.ElementType; appKey?: AppKey; badge?: number };
type NavGroup = { type: "group"; label: string; icon: React.ElementType; items: NavDef[] };
type NavDef = NavItem | NavGroup;

function firstNavigationPath(nodes: NavDef[], activeAppKey: AppKey | "all"): string | undefined {
  for (const node of nodes) {
    if (node.type === "item") {
      if (activeAppKey === "all" || !node.appKey || node.appKey === activeAppKey) return node.path;
      continue;
    }
    const path = firstNavigationPath(node.items, activeAppKey);
    if (path) return path;
  }
  return undefined;
}

const ICONS: Record<string, React.ElementType> = {
  MessageSquare, History, BarChart2, Calculator, Cpu, Server, BookOpen, Puzzle,
  Clock, Users, Settings, ScrollText, Zap, Wrench, Home, Workflow,
  Play, Inbox, CalendarClock, Radio, ShieldCheck, KeyRound, Lock, UsersRound,
  PanelTop, ListTree,
};

function navNode(node: Record<string, any>, pages: RegisteredPage[]): NavDef | null {
  const icon = ICONS[node.icon] ?? Home;
  if (node.type === "group") {
    const items = (node.items ?? []).map((item: Record<string, any>) => navNode(item, pages))
      .filter((item: NavDef | null): item is NavDef => item !== null);
    return items.length ? { type: "group", label: node.label, icon, items } : null;
  }
  if (node.type === "item" && node.path) {
    const page = pages.find((candidate) => candidate.key === node.target);
    return { type: "item", path: node.path, label: node.label, icon, appKey: page?.app_key };
  }
  return null;
}

const PATH_LABELS: Record<string, string> = {
  chat: "Chat", sessions: "Sessions", analytics: "Analytics",
  tokenomics: "Tokenomics", report: "Report", estimate: "Estimation",
  models: "Models",   mcp: "MCP Servers", agents: "Agents", skills: "Skills",
  plugins: "Plugins", scheduler: "Scheduler", profiles: "Profiles",
  routing: "Routing", guardrail: "Guardrail",
  config: "Configurations", logs: "Logs",
  applications: "Applications", manage: "Manage",
  flows: "Flow", instances: "Instances", tasks: "My Tasks", definitions: "Definitions",
  deployments: "Deployments", events: "Events",
  security: "Security", "service-accounts": "Service Accounts", secrets: "Secret Store",
  users: "Users & Groups", roles: "Roles", developer: "Developer",
  functions: "Functions", menus: "Menus", "menu-items": "Menu Items",
};

const SIDEBAR_W  = 248;
const SIDEBAR_CW = 56;  // collapsed width

// ── Styles ────────────────────────────────────────────────────────────────────

const S = {
  shell: { display:"flex", height:"100vh", background:"var(--bg)", overflow:"hidden" } as React.CSSProperties,

  sidebar: (collapsed: boolean): React.CSSProperties => ({
    width: collapsed ? SIDEBAR_CW : SIDEBAR_W,
    flexShrink: 0,
    background: "var(--sidebar-bg)",
    borderRight: "1px solid var(--sidebar-border)",
    display: "flex", flexDirection: "column",
    overflow: "hidden",
    transition: "width 0.2s ease",
  }),

  logo: (collapsed: boolean): React.CSSProperties => ({
    height: "56px", padding: collapsed ? "0 14px" : "0 16px",
    display: "flex", alignItems: "center",
    justifyContent: collapsed ? "center" : "space-between",
    gap: "10px",
    borderBottom: "1px solid var(--sidebar-border)", flexShrink: 0,
  }),
  logoLeft: { display:"flex", alignItems:"center", gap:"10px" } as React.CSSProperties,
  logoIcon: {
    width: "28px", height: "28px", borderRadius: "8px",
    background: "var(--primary)",
    display: "flex", alignItems: "center", justifyContent: "center",
    flexShrink: 0,
  } as React.CSSProperties,
  logoText: { fontWeight:700, fontSize:"14px", color:"var(--sidebar-fg)", letterSpacing:"-0.01em", whiteSpace:"nowrap" as const },
  logoSub:  { fontSize:"11px", color:"var(--sidebar-muted)", lineHeight:1.3 },

  toggleBtn: {
    background: "transparent", border: "none", cursor: "pointer",
    color: "var(--sidebar-muted)", padding: "4px", borderRadius: "6px",
    display: "flex", alignItems: "center", justifyContent: "center",
    flexShrink: 0,
    transition: "color 0.1s, background 0.1s",
  } as React.CSSProperties,

  navScroll: { flex:1, overflowY:"auto" as const, padding:"8px 0" },

  groupLabel: (collapsed: boolean): React.CSSProperties => ({
    display: collapsed ? "none" : "flex",
    alignItems: "center", justifyContent: "space-between",
    padding: "6px 16px", marginTop: "4px",
    fontSize: "11px", fontWeight: 600,
    letterSpacing: "0.02em", color: "var(--sidebar-muted)",
    cursor: "pointer", userSelect: "none",
    transition: "color 0.1s",
  }),

  // collapsed group separator — tiny icon only
  groupSep: {
    height: "1px", background: "var(--sidebar-border)",
    margin: "6px 12px",
  } as React.CSSProperties,

  navItem: (active: boolean, collapsed: boolean): React.CSSProperties => ({
    display: "flex", alignItems: "center",
    gap: collapsed ? 0 : "10px",
    padding: collapsed ? "8px 0" : "7px 16px",
    margin: collapsed ? "1px 6px" : "1px 8px",
    justifyContent: collapsed ? "center" : "flex-start",
    borderRadius: "8px",
    fontSize: "13px", fontWeight: active ? 600 : 400,
    color: active ? "var(--sidebar-fg)" : "var(--sidebar-muted)",
    background: active ? "var(--nav-active)" : "transparent",
    textDecoration: "none",
    cursor: "pointer",
    transition: "background 0.1s, color 0.1s",
    borderLeft: (!collapsed && active) ? "2px solid var(--sidebar-fg)" : "2px solid transparent",
    outline: (collapsed && active) ? "2px solid var(--sidebar-fg)" : "none",
    outlineOffset: collapsed ? "-2px" : "0",
    position: "relative" as const,
  }),

  userCard: (collapsed: boolean): React.CSSProperties => ({
    padding: collapsed ? "12px 0" : "12px 16px",
    borderTop: "1px solid var(--sidebar-border)",
    display: "flex", alignItems: "center",
    justifyContent: collapsed ? "center" : "flex-start",
    gap: "10px", flexShrink: 0,
  }),
  avatar: {
    width: "32px", height: "32px", borderRadius: "50%",
    background: "var(--nav-hover)",
    display: "flex", alignItems: "center", justifyContent: "center",
    fontSize: "12px", fontWeight: 700, color: "var(--sidebar-fg)", flexShrink: 0,
  } as React.CSSProperties,
  userName: { fontSize:"13px", fontWeight:600, color:"var(--sidebar-fg)", lineHeight:1.3, whiteSpace:"nowrap" as const },
  userOrg:  { fontSize:"11px", color:"var(--sidebar-muted)", lineHeight:1.3, whiteSpace:"nowrap" as const },

  main:    { flex:1, display:"flex", flexDirection:"column" as const, overflow:"hidden" },

  topbar: {
    height: "56px", flexShrink: 0,
    borderBottom: "1px solid var(--border)",
    display: "flex", alignItems: "center", padding: "0 24px",
    background: "var(--header-bg)", gap: "6px",
  } as React.CSSProperties,
  crumb:    { fontSize:"13px", color:"var(--fg-muted)" },
  crumbSep: { fontSize:"13px", color:"var(--fg-subtle)", margin:"0 2px" },
  crumbCur: { fontSize:"13px", fontWeight:500, color:"var(--fg)" },

  content: { flex:1, minWidth:0, minHeight:0, boxSizing:"border-box", padding:12, overflow:"hidden", display:"flex", flexDirection:"column" as const } as React.CSSProperties,

  navBadge: {
    fontSize: "10.5px", fontWeight: 700, color: "var(--danger-action-fg)", background: "var(--danger-action-bg)",
    borderRadius: "9px", padding: "1px 6px", minWidth: "16px", textAlign: "center" as const,
    flexShrink: 0,
  } as React.CSSProperties,
  navBadgeDot: {
    position: "absolute" as const, top: "6px", right: "6px",
    width: "7px", height: "7px", borderRadius: "50%", background: "#ef4444",
  } as React.CSSProperties,

  // tooltip shown when collapsed
  tooltip: {
    position: "absolute" as const,
    left: "calc(100% + 10px)",
    top: "50%", transform: "translateY(-50%)",
    background: "var(--tooltip-bg)",
    border: "1px solid var(--border)",
    color: "var(--tooltip-fg)",
    fontSize: "12px", fontWeight: 500,
    padding: "4px 8px", borderRadius: "5px",
    whiteSpace: "nowrap" as const,
    pointerEvents: "none" as const,
    zIndex: 999,
    boxShadow: "var(--shadow-soft)",
  },
};

// ── Components ────────────────────────────────────────────────────────────────

function NavItemLink({ item, collapsed, indent = 0, activeAppKey }: {
  item: NavItem; collapsed: boolean; indent?: number; activeAppKey: AppKey | "all";
}) {
  const [hovered, setHovered] = useState(false);

  return (
    <NavLink
      to={item.path}
      reloadDocument={activeAppKey !== "all" && !!item.appKey && item.appKey !== activeAppKey}
      title={collapsed ? item.label : undefined}
      style={({ isActive }) => ({
        ...S.navItem(isActive, collapsed),
        // Hover and selection share one inset so the highlight never jumps
        // across the nearest tree guide as the pointer moves.
        paddingLeft: collapsed ? "0px" : (isActive || hovered) && indent > 0
          ? "4px" : `${16 + indent * 16}px`,
        marginLeft: collapsed ? "6px" : (isActive || hovered) && indent > 0
          ? `${navGuideX(indent - 1) + 4}px` : "8px",
        background: hovered && !isActive
          ? "var(--nav-hover)"
          : isActive ? "var(--nav-active)" : "transparent",
      })}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <item.icon size={15} strokeWidth={1.75} style={{ flexShrink: 0 }} />
      {!collapsed && <span style={{ flex: 1 }}>{item.label}</span>}
      {!collapsed && !!item.badge && (
        <span style={S.navBadge}>{item.badge}</span>
      )}
      {collapsed && !!item.badge && (
        <span style={S.navBadgeDot} />
      )}
      {/* Tooltip when collapsed + hovered */}
      {collapsed && hovered && (
        <span style={S.tooltip}>{item.label}</span>
      )}
    </NavLink>
  );
}

function navGroupIsActive(group: NavGroup, pathname: string): boolean {
  return group.items.some(i =>
    i.type === "item" ? pathname.startsWith(i.path) : navGroupIsActive(i, pathname)
  );
}

function navGuideX(depth: number): number {
  // Center the guide on the menu icon; the wider level step clears the next
  // level's icon column.
  return 32 + depth * 16;
}

function NavGroupSection({ group, collapsed, depth = 0, activeAppKey, connectFromX, maskAncestorX, isLastChild = false }: {
  group: NavGroup; collapsed: boolean; depth?: number; activeAppKey: AppKey | "all";
  connectFromX?: number; maskAncestorX?: number; isLastChild?: boolean;
}) {
  const location = useLocation();
  const anyActive = navGroupIsActive(group, location.pathname);
  // A nested subgroup (e.g. Security under Administration) starts collapsed
  // — only the top-level group defaults open — so opening the sidebar
  // doesn't immediately dump every subsection's items into view at once.
  const [open, setOpen] = useState(depth === 0);

  return (
    <div style={{ position: "relative" }}>
      {collapsed
        // Only the top-level group gets a separator when the sidebar is
        // collapsed; a nested one would just add a redundant divider between
        // icons that are already visually grouped by their parent's.
        ? (depth === 0 && <div style={S.groupSep} />)
        : (
          <div
            // Align submenu icons with function icons in the same child
            // column; the additional level spacing leaves room for guides.
            style={{ ...S.groupLabel(collapsed), color: anyActive ? "var(--sidebar-fg)" : "var(--sidebar-muted)", paddingLeft: `${26 + depth * 16}px` }}
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
      {(open || collapsed) && group.items.map((item, index) => (
        item.type === "item"
          ? <div key={item.path} style={{ position: "relative", display: "flow-root" }}>
              <NavItemLink item={item} collapsed={collapsed} indent={depth + 1} activeAppKey={activeAppKey} />
              {!collapsed && <div aria-hidden="true" style={{
                position: "absolute", pointerEvents: "none", zIndex: 1,
                left: navGuideX(depth), width: 10, top: "50%",
                borderTop: "1px solid var(--sidebar-border)",
              }} />}
          </div>
          : <NavGroupSection key={item.label} group={item} collapsed={collapsed} depth={depth + 1} activeAppKey={activeAppKey}
              connectFromX={navGuideX(depth)}
              maskAncestorX={navGuideX(depth)}
              isLastChild={index === group.items.length - 1} />
      ))}
      {!collapsed && connectFromX !== undefined && (
        <div aria-hidden="true" style={{
          position: "absolute", pointerEvents: "none", zIndex: 1,
          left: connectFromX,
          width: Math.max(0, navGuideX(depth) - 6 - connectFromX),
          top: 16, borderTop: "1px solid var(--sidebar-border)",
        }} />
      )}
      {!collapsed && maskAncestorX !== undefined && (
        <div aria-hidden="true" style={{
          position: "absolute", pointerEvents: "none", zIndex: 2,
          // Keep the parent's spine through a T junction for a submenu with
          // later siblings. A final submenu turns the spine at the junction
          // and ends as an L.
          left: maskAncestorX - 1, top: isLastChild ? 17 : 29, bottom: 2, width: 3,
          background: "var(--sidebar-bg)",
        }} />
      )}
      {!collapsed && open && (
        <div
          aria-hidden="true"
          style={{
            position: "absolute", pointerEvents: "none", zIndex: 1,
            left: navGuideX(depth), top: 22,
            bottom: group.items.at(-1)?.type === "item" ? 16 : 3,
            borderLeft: "1px solid var(--sidebar-border)",
          }}
        />
      )}
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
      <span id="flow-breadcrumb-tail" />
    </div>
  );
}

/** Wraps non-chat pages so they scroll independently without affecting the outer shell. */
function ScrollPage({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ flex: 1, minWidth: 0, minHeight: 0, overflow: "auto" }}>
      {children}
    </div>
  );
}

// ── App ───────────────────────────────────────────────────────────────────────

interface AuthStatus { login_required: boolean; authenticated: boolean; must_change_password: boolean; oidc_configured: boolean; username?: string | null }

async function loadAuthStatus(): Promise<AuthStatus> {
  const base = window.__GYRFALCON_BASE_PATH__ || "";
  const response = await fetch(`${base}/auth/status`, { credentials: "same-origin", cache: "no-store" });
  if (!response.ok) throw new Error("Could not check sign-in status");
  return response.json();
}

export function App({ pageRegistry, activeAppKey = "all" }: {
  pageRegistry: ReadonlyMap<string, PageRegistration>;
  activeAppKey?: AppKey | "all";
}) {
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [authError, setAuthError] = useState(false);
  const refreshAuth = () => loadAuthStatus().then(value => { setAuth(value); setAuthError(false); }).catch(() => setAuthError(true));
  useEffect(() => { void refreshAuth(); }, []);

  if (authError) return <div style={{ padding: 32, color: "var(--fg)" }}>Could not connect to the Gyrfalcon server. Reload to try again.</div>;
  if (!auth) return <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", color: "var(--fg-muted)" }}>Loading sign-in…</div>;
  if (auth.login_required && !auth.authenticated) return <LoginPage onSignedIn={() => void refreshAuth()} oidcConfigured={auth.oidc_configured} />;
  if (auth.must_change_password) return <ChangePasswordPage onChanged={() => void refreshAuth()} />;
  return <DashboardApp pageRegistry={pageRegistry} activeAppKey={activeAppKey} username={auth.username || "User"} onLogout={async () => {
    const base = window.__GYRFALCON_BASE_PATH__ || "";
    await fetch(`${base}/auth/logout`, { method: "POST", credentials: "same-origin" });
    await refreshAuth();
  }} />;
}

function DashboardApp({ username, onLogout, pageRegistry, activeAppKey }: {
  username: string;
  onLogout: () => void;
  pageRegistry: ReadonlyMap<string, PageRegistration>;
  activeAppKey: AppKey | "all";
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [roles, setRoles] = useState<string[] | null>(null);
  const [navigation, setNavigation] = useState<NavigationPayload | null>(null);
  useEffect(() => {
    api.getCurrentUser().then((user) => setRoles(user.roles)).catch(() => setRoles([]));
    api.getNavigation().then(setNavigation).catch(() => setNavigation({ roots: [], pages: [], grants: {} }));
  }, []);
  const canDesignFlows = roles?.some((role) => ["admin", "system_admin", "app_developer", "operator"].includes(role)) ?? false;

  // Badge "My Tasks" with the pending-approval count — per §14.2, this plus the
  // page itself is the entire discoverability story for human-in-the-loop.
  const [pendingTaskCount, setPendingTaskCount] = useState(0);
  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      api.getFlowInbox()
        .then((d) => { if (!cancelled) setPendingTaskCount((d.notifications || []).length); })
        .catch(() => {});
    };
    poll();
    const id = setInterval(poll, 20000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const navWithBadges: NavDef[] = (navigation?.roots ?? []).map((root) => ({
    type: "group",
    label: root.label,
    icon: ICONS[root.icon ?? ""] ?? Home,
    items: root.items.map((item) => navNode(item, navigation?.pages ?? []))
      .filter((item): item is NavDef => item !== null)
      .map((item) => {
        if (item.type !== "group") return item;
        return {
          ...item,
          items: item.items.map((child) => child.type === "item" && child.path === "/flows/tasks"
            ? { ...child, badge: pendingTaskCount || undefined }
            : child),
        };
      }),
  }));
  const landingPath = firstNavigationPath(navWithBadges, activeAppKey);
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
                <span style={{ color:"var(--btn-fg)", fontWeight:900, fontSize:"13px" }}>G</span>
              </div>
              <div>
                <div style={S.logoText}>{APP_NAME}</div>
                <div style={S.logoSub}>{APP_SUBTITLE}</div>
              </div>
            </div>
          )}
          {collapsed && (
            <div style={S.logoIcon}>
              <span style={{ color:"var(--btn-fg)", fontWeight:900, fontSize:"13px" }}>G</span>
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
          {navWithBadges.map((def, index) =>
            def.type === "item"
              ? <NavItemLink key={`${def.path}-${index}`} item={def} collapsed={collapsed} activeAppKey={activeAppKey} />
              : <NavGroupSection key={`${def.label}-${index}`} group={def} collapsed={collapsed} activeAppKey={activeAppKey} />
          )}
        </div>

        {/* Expand button (collapsed mode only) */}
        {collapsed && (
          <div style={{ padding:"8px 0", display:"flex", justifyContent:"center", borderTop:"1px solid var(--sidebar-border)" }}>
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
          <div style={S.avatar} title={collapsed ? `${username} · ${ORG_NAME}` : undefined}>
            {username.slice(0, 2).toUpperCase()}
          </div>
          {!collapsed && (
            <>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={S.userName}>{username}</div>
                <div style={S.userOrg}>{ORG_NAME}</div>
              </div>
              <button
                onClick={onLogout}
                title="Sign out"
                style={{ background: "transparent", border: "1px solid var(--sidebar-border)", borderRadius: "8px", padding: "5px 6px", cursor: "pointer", color: "var(--sidebar-muted)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}
              ><LogOut size={14} /></button>
              <button
                onClick={() => setIsDark(d => !d)}
                title={isDark ? "Switch to light theme" : "Switch to dark theme"}
                style={{
                  background: "transparent",
                  border: "1px solid var(--sidebar-border)",
                  borderRadius: "8px",
                  padding: "5px 6px",
                  cursor: "pointer",
                  color: "var(--sidebar-muted)",
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
            <button onClick={onLogout} title="Sign out" style={{ background: "transparent", border: "none", cursor: "pointer", color: "var(--sidebar-muted)", display: "flex", alignItems: "center", padding: "4px" }}>
              <LogOut size={13} />
            </button>
          )}
          {collapsed && (
            <button
              onClick={() => setIsDark(d => !d)}
              title={isDark ? "Switch to light theme" : "Switch to dark theme"}
              style={{
                background: "transparent", border: "none",
                cursor: "pointer", color: "var(--sidebar-muted)",
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
          <div id="flow-breadcrumb-actions" style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 5 }} />
        </header>
        <div style={S.content}>
          <Routes>
            <Route path="/" element={landingPath
              ? <Navigate to={landingPath} replace />
              : <div style={{ padding: 16 }}>{navigation ? "No pages are assigned to this user." : "Loading navigation…"}</div>}
            />
            {(navigation?.pages ?? []).map((page) => {
              const registration = pageRegistry.get(page.key);
              if (!registration || !page.enabled || page.app_key !== registration.appKey
                  || (activeAppKey !== "all" && page.app_key !== activeAppKey)) return null;
              const grantKey = registration.grantKey ?? page.key;
              const granted = Object.values(navigation?.grants ?? {}).some(
                (grant) => grant.kind === "page" && grant.target === grantKey,
              );
              if (!granted) return null;
              const PageComponent = registration.component;
              let content: React.ReactNode = <PageComponent />;
              if (registration.requiresFlowDesign) {
                content = roles === null
                  ? <div style={{ padding: 16 }}>Checking access…</div>
                  : canDesignFlows ? content
                  : <div style={{ padding: 16 }}>Administrator or developer role required.</div>;
              }
              if (registration.scroll) content = <ScrollPage>{content}</ScrollPage>;
              return <Route key={page.key} path={page.route} element={content} />;
            })}
          </Routes>
        </div>
      </div>
    </div>
  );
}
