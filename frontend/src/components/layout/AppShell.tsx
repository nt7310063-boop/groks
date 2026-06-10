import { useEffect, useMemo, useState } from "react";
import { Link, Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { ChevronDown, LogOut, Menu, X, Globe } from "lucide-react";

import { useAuthStore } from "@/core/auth/store";
import { useDomainStore } from "@/core/domain/store";
import type { NavEntry, NavLeaf, NavGroup } from "@/app/types";
import { useTranslation } from "react-i18next";

import { getAuthedNav } from "@/app/moduleRegistry";
import { useDocumentTitle } from "@/core/useDocumentTitle";
import { NotificationBell } from "./NotificationBell";
import { MaintenanceBanner } from "@/components/ui/MaintenanceBanner";
import { SubscriptionBanner } from "@/components/layout/SubscriptionBanner";
const NAV_KEY_I18N: Record<string, string> = {
  auth: "nav.auth",
  web: "nav.web",
  grok: "nav.grok",
  flow: "nav.flow",
  gateway: "nav.gateway",
};

export function AppShell() {
  useDocumentTitle();
  const { user, clear } = useAuthStore();

  const baseNav: NavEntry[] = useMemo(() => getAuthedNav(user?.role), [user?.role]);
  const isSuper = user?.role === "super_admin";

  const visibleNav: NavEntry[] = useMemo(() => {
    return baseNav;
  }, [baseNav]);

  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();

  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  const navigate = useNavigate();

  const onLogout = () => {
    clear();
    navigate("/login");
  };

  const domainConfig = useDomainStore((s) => s.config);
  const brandName = domainConfig?.brand_name ?? "GrokFlow";

  return (
    <div className="flex h-screen w-screen bg-slate-100 overflow-hidden text-slate-700">
      {/* Desktop sidebar */}
      <aside className="hidden md:flex flex-col w-64 bg-white border-r border-slate-200 shrink-0">
        <div className="h-16 px-6 flex items-center border-b border-slate-200">
          <Link to="/" className="flex items-center gap-2 group min-w-0">
            <span className="w-8 h-8 rounded-lg bg-blue-600 text-white flex items-center justify-center font-bold shrink-0 shadow-md group-hover:bg-blue-700 transition-colors">
              {(brandName?.[0] ?? "G").toUpperCase()}
            </span>
            <span className="font-bold text-lg text-slate-800 truncate group-hover:text-blue-600 transition-colors">
              {brandName}
            </span>
          </Link>
        </div>
        <nav className="p-3 flex-1 overflow-y-auto space-y-0.5">
          <NavAccordion items={visibleNav} currentPath={location.pathname} />
        </nav>
        {user && (
          <div className="border-t border-slate-200 p-3 space-y-2">
            <div className="flex items-center gap-2.5 px-2 py-1.5">
              <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-sky-500 to-blue-600 text-white flex items-center justify-center font-semibold text-sm shrink-0 shadow-sm">
                {(user.email?.[0] ?? "U").toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <p className="text-xs font-semibold text-slate-800 truncate">{user.email}</p>
                <p className="text-[10px] text-slate-500 uppercase tracking-wider">{user.role}</p>
              </div>
            </div>
          </div>
        )}
      </aside>

      {/* Mobile sidebar overlay */}
      {mobileOpen && (
        <div 
          className="fixed inset-0 z-40 bg-slate-900/40 backdrop-blur-sm md:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* Mobile sidebar */}
      <aside className={`fixed inset-y-0 left-0 z-50 w-64 bg-white border-r border-slate-200 flex flex-col md:hidden transform transition-transform duration-200 ${mobileOpen ? "translate-x-0" : "-translate-x-full"}`}>
        <div className="h-16 px-6 flex items-center justify-between border-b border-slate-200">
          <Link to="/" className="flex items-center gap-2 group min-w-0">
            <span className="w-8 h-8 rounded-lg bg-blue-600 text-white flex items-center justify-center font-bold shrink-0 shadow-md group-hover:bg-blue-700 transition-colors">
              {(brandName?.[0] ?? "G").toUpperCase()}
            </span>
            <span className="font-bold text-lg text-slate-800 truncate group-hover:text-blue-600 transition-colors">
              {brandName}
            </span>
          </Link>
          <button
            type="button"
            onClick={() => setMobileOpen(false)}
            className="md:hidden -mr-1 p-1.5 rounded-lg text-slate-500 hover:text-slate-800 hover:bg-slate-100"
            aria-label="Close menu"
          >
            <X size={18} />
          </button>
        </div>
        <nav className="p-3 flex-1 overflow-y-auto space-y-0.5">
          <NavAccordion items={visibleNav} currentPath={location.pathname} />
        </nav>
        {user && (
          <div className="border-t border-slate-200 p-3 space-y-2">
            <div className="flex items-center gap-2.5 px-2 py-1.5">
              <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-sky-500 to-blue-600 text-white flex items-center justify-center font-semibold text-sm shrink-0 shadow-sm">
                {(user.email?.[0] ?? "U").toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <p className="text-xs font-semibold text-slate-800 truncate">{user.email}</p>
                <p className="text-[10px] text-slate-500 uppercase tracking-wider">{user.role}</p>
              </div>
            </div>
          </div>
        )}
      </aside>

      <div className="flex-1 flex flex-col overflow-hidden min-w-0">
        <MaintenanceBanner />
        <SubscriptionBanner />
        <header className="sticky top-0 z-20 bg-white/85 backdrop-blur border-b border-slate-200/70 px-3 sm:px-4 md:px-6 py-3 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <button
              type="button"
              onClick={() => setMobileOpen(true)}
              className="md:hidden p-2 -ml-1 text-slate-600 hover:text-slate-800 rounded-lg hover:bg-slate-100"
              aria-label="Open menu"
            >
              <Menu size={20} />
            </button>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <NotificationBell />
            <button onClick={onLogout} className="btn-ghost btn-sm" aria-label="Logout">
              <LogOut size={15} />
              <span className="hidden sm:inline">Logout</span>
            </button>
          </div>
        </header>
        <main className="flex-1 overflow-auto p-4 sm:p-5 md:p-7 animate-fade-in">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function LeafLink({ item }: { item: NavLeaf }) {
  const Icon = item.icon;
  const location = useLocation();
  const [toPath, toQs = ""] = item.to.split("?");
  const isExact = item.to === "/dashboard";
  const samePath = isExact
    ? location.pathname === toPath
    : location.pathname === toPath || location.pathname.startsWith(toPath + "/");
  const sameQs = toQs === ""
    ? true
    : new URLSearchParams(location.search).toString() === new URLSearchParams(toQs).toString();
  const isActive = samePath && sameQs;
  return (
    <Link
      to={item.to}
      className={`relative flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-all duration-150 ${
        isActive
          ? "bg-blue-50 text-blue-700"
          : "text-slate-600 hover:bg-slate-100 hover:text-slate-800"
      }`}
    >
      {isActive && (
        <span className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-5 rounded-r-full bg-blue-600" />
      )}
      <Icon size={16} className={isActive ? "text-blue-600" : ""} />
      <span className="truncate">{item.label}</span>
    </Link>
  );
}

function groupHasActiveLeaf(items: NavEntry[], currentPath: string): boolean {
  return items.some((it) => {
    if (it.type === "link") return currentPath.startsWith(it.to);
    return groupHasActiveLeaf(it.items, currentPath);
  });
}

function NavAccordion({
  items, currentPath,
}: { items: NavEntry[]; currentPath: string }) {
  const activeKey = (() => {
    for (const it of items) {
      if (it.type === "group" && groupHasActiveLeaf(it.items, currentPath)) {
        return it.key;
      }
    }
    return null;
  })();
  const [openKey, setOpenKey] = useState<string | null>(activeKey);

  useEffect(() => {
    if (activeKey) setOpenKey(activeKey);
  }, [activeKey]);

  return (
    <>
      {items.map((item) =>
        item.type === "link" ? (
          <LeafLink key={item.to} item={item} />
        ) : (
          <CollapsibleGroup
            key={item.key}
            group={item}
            currentPath={currentPath}
            isOpen={openKey === item.key}
            onToggle={() => setOpenKey((k) => (k === item.key ? null : item.key))}
          />
        )
      )}
    </>
  );
}

function CollapsibleGroup({
  group, currentPath, isOpen, onToggle,
}: {
  group: NavGroup;
  currentPath: string;
  isOpen: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const Icon = group.icon;
  const hasActive = groupHasActiveLeaf(group.items, currentPath);
  const i18nKey = NAV_KEY_I18N[group.key];
  const label = i18nKey ? t(i18nKey, group.label) : group.label;

  return (
    <div>
      <button
        type="button"
        onClick={onToggle}
        className={`w-full flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-all ${
          hasActive
            ? "text-slate-800 bg-slate-100"
            : "text-slate-600 hover:bg-slate-100 hover:text-slate-800"
        }`}
      >
        <Icon size={16} className={hasActive ? "text-blue-600" : ""} />
        <span className="flex-1 text-left">{label}</span>
        <ChevronDown
          size={14}
          className={`transition-transform ${isOpen ? "rotate-0" : "-rotate-90"}`}
        />
      </button>
      {isOpen && (
        <div className="ml-3.5 pl-3 border-l-2 border-slate-200 mt-1 space-y-0.5 animate-slide-up">
          <NavAccordion items={group.items} currentPath={currentPath} />
        </div>
      )}
    </div>
  );
}
