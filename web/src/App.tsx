import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import {
  Activity,
  ArrowUpRight,
  Bot,
  ChevronDown,
  Database,
  FlaskConical,
  GitMerge,
  GitPullRequest,
  LayoutDashboard,
  LogOut,
  Menu,
  Monitor,
  Moon,
  PackageOpen,
  Radio,
  Send,
  Sun,
  X,
} from "lucide-react";
import { isMock, useApi } from "./api/client";
import { Button } from "./components/ui/button";
import {
  DropdownMenuRoot,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "./components/ui/dropdown-menu";
import { RelativeTime } from "./components/common";
import { absoluteTime, sentenceCase } from "./lib/utils";
import { StatusBadge } from "./components/status";
import { OverviewPage } from "./pages/overview";
import { BatchPage } from "./pages/batch";
import { QueuePage } from "./pages/queue";
import {
  LedgerPage,
  AgentsPage,
  OutboxPage,
  QueryPage,
} from "./pages/operations";
import { ReleasesPage } from "./pages/releases";
const navigation = [
  { label: "Overview", to: "/", icon: LayoutDashboard },
  { label: "Pull request queue", to: "/queue", icon: GitPullRequest },
  { label: "CI failures", to: "/ledger", icon: FlaskConical },
  { label: "Agents", to: "/agents", icon: Bot },
  { label: "Outbox", to: "/outbox", icon: Send },
  { label: "Releases", to: "/releases", icon: PackageOpen },
  { label: "Query console", to: "/query", icon: Database },
];
type Theme = "light" | "dark" | "system";
export default function App() {
  const me = useApi("/me");
  const health = useApi("/health");
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem("mm-theme") as Theme) || "system",
  );
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.classList.toggle(
        "dark",
        theme === "dark" || (theme === "system" && media.matches),
      );
      document.documentElement.dataset.theme = theme;
      localStorage.setItem("mm-theme", theme);
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);
  useEffect(() => {
    setMenuOpen(false);
    document.title = `${navigation.find((n) => n.to === location.pathname)?.label ?? "Batch detail"} · Merge Marshall`;
  }, [location.pathname]);
  return (
    <div className="app-shell">
      <a href="#main-content" className="skip-link">
        Skip to content
      </a>
      <aside className={`sidebar ${menuOpen ? "is-open" : ""}`}>
        <div className="sidebar-content">
          <NavLink to="/" className="brand">
            <span className="brand-mark">
              <GitMerge size={19} />
            </span>
            <span>
              Merge Marshall<small>Bifrost</small>
            </span>
          </NavLink>
          <div className="sidebar-project">
            <span className="project-avatar">B</span>
            <div className="min-w-0">
              <strong>BrokkAi/bifrost-dev</strong>
            </div>
          </div>
          <div className="nav-caption">Workspace</div>
          <nav aria-label="Main navigation">
            {navigation.map(({ label, to, icon: Icon }) => (
              <NavLink
                end={to === "/"}
                key={to}
                to={to}
                className={({ isActive }) =>
                  `nav-item ${isActive ? "active" : ""}`
                }
              >
                <Icon size={17} />
                <span>{label}</span>
              </NavLink>
            ))}
          </nav>
          <div className="sidebar-bottom">
            <a
              href="https://github.com/BrokkAi/bifrost-ci-monitor"
              target="_blank"
              rel="noreferrer"
              className="repo-link"
            >
              View repository
              <ArrowUpRight size={13} />
            </a>
            <div className="text-[10px] text-muted-foreground mt-3">
              {isMock ? "Preview workspace · mock data" : "Merge Marshall · v1"}
            </div>
          </div>
        </div>
      </aside>
      {menuOpen && (
        <button
          className="sidebar-backdrop"
          aria-label="Close navigation"
          onClick={() => setMenuOpen(false)}
        />
      )}
      <div className="workspace">
        <header className="topbar">
          <div className="flex items-center gap-3">
            <Button
              variant="ghost"
              size="icon"
              className="mobile-menu"
              onClick={() => setMenuOpen(!menuOpen)}
              aria-label="Toggle navigation"
            >
              {menuOpen ? <X /> : <Menu />}
            </Button>
            <span className="topbar-project">bifrost-dev</span>
            <span className="text-border">/</span>
            <span className="text-xs text-muted-foreground">
              {navigation.find((n) => n.to === location.pathname)?.label ??
                "Batch detail"}
            </span>
          </div>
          <div className="flex items-center gap-3">
            {isMock && (
              <div className="desktop-health-label">
                <Radio size={12} />
                Preview
              </div>
            )}
            <DropdownMenuRoot>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Theme: ${theme}`}
                >
                  <Sun className="dark:hidden" />
                  <Moon className="hidden dark:block" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {(
                  [
                    { value: "system", icon: Monitor },
                    { value: "light", icon: Sun },
                    { value: "dark", icon: Moon },
                  ] as const
                ).map(({ value, icon: Icon }) => (
                  <DropdownMenuItem
                    key={value}
                    onSelect={() => setTheme(value)}
                  >
                    <Icon />
                    {value[0].toUpperCase() + value.slice(1)}
                    {theme === value && <span className="ml-auto">✓</span>}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenuRoot>
            <DropdownMenuRoot>
              <DropdownMenuTrigger asChild>
                <button className="user-trigger">
                  <span className="user-avatar">
                    {(me.data?.login ?? "…").slice(0, 1).toUpperCase()}
                  </span>
                  <span className="user-name">
                    {me.data?.login ?? "Loading…"}
                  </span>
                  <ChevronDown size={12} />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <div className="px-3 py-2">
                  <div className="text-sm font-medium">{me.data?.login}</div>
                  <div className="text-xs text-muted-foreground mt-1">
                    {sentenceCase(me.data?.permission)} access
                  </div>
                </div>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onSelect={async () => {
                    if (isMock) return;
                    await fetch("/auth/logout", {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: "{}",
                    });
                    window.location.assign("/auth/login");
                  }}
                >
                  <LogOut />
                  Sign out{isMock && " (preview)"}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenuRoot>
          </div>
        </header>
        <div className="health-strip" aria-label="System health">
          <span className="health-caption">
            <Activity size={13} />
            System health
          </span>
          {health.data?.entry_points.map((entry) => (
            <div
              className="health-item"
              key={entry.name}
              title={
                entry.last_error ??
                `Last completed tick: ${absoluteTime(entry.last_finished_at)}`
              }
            >
              <StatusBadge
                value={entry.state}
                label={`${entry.name[0].toUpperCase() + entry.name.slice(1)} · ${entry.state === "ok" ? "healthy" : entry.state}`}
                className="health-status"
              />
              <span className="health-time">
                <RelativeTime value={entry.last_finished_at} />
              </span>
            </div>
          ))}
          {health.data && (
            <StatusBadge
              value={health.data.skill_service.reachable ? "ok" : "failing"}
              label={`Skill service · ${health.data.skill_service.reachable ? "online" : "unreachable"}`}
              className="health-status"
            />
          )}
          {health.isError && (
            <span className="text-xs text-muted-foreground">
              Health unavailable
            </span>
          )}
        </div>
        <main id="main-content">
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/batches/:id" element={<BatchPage />} />
            <Route path="/queue" element={<QueuePage />} />
            <Route path="/ledger" element={<LedgerPage />} />
            <Route path="/agents" element={<AgentsPage />} />
            <Route path="/outbox" element={<OutboxPage />} />
            <Route path="/releases" element={<ReleasesPage />} />
            <Route path="/query" element={<QueryPage />} />
            <Route
              path="*"
              element={
                <div className="py-20 text-center">
                  <h1 className="text-xl font-semibold">Page not found</h1>
                  <NavLink to="/" className="external-link mt-4">
                    Return to overview
                  </NavLink>
                </div>
              }
            />
          </Routes>
          <footer className="page-footer">
            <span>
              Merge Marshall <span className="mx-1.5">·</span> BrokkAi /
              bifrost-dev
            </span>
            <span>
              Refreshes every 15 seconds <span className="refresh-dot" />
            </span>
          </footer>
        </main>
      </div>
    </div>
  );
}
