"use client";

import { useState, useEffect, useCallback, useSyncExternalStore } from "react";
import {
  BarChart3,
  ScanSearch,
  MessageSquare,
  FileText,
  Activity,
  Settings,
  Menu,
  X,
  ClipboardList,
  PieChart,
  ListChecks,
  FlaskConical,
} from "lucide-react";
import { NavLink } from "@/components/molecules/NavLink";
import { ThemeToggle } from "@/components/atoms/ThemeToggle";

const MD_BREAKPOINT = "(min-width: 768px)";

function subscribeMediaQuery(callback: () => void) {
  const mq = window.matchMedia(MD_BREAKPOINT);
  mq.addEventListener("change", callback);
  return () => mq.removeEventListener("change", callback);
}

function getIsDesktop() {
  return window.matchMedia(MD_BREAKPOINT).matches;
}

function getIsDesktopServer() {
  return false;
}

export function Sidebar() {
  const [open, setOpen] = useState(false);
  const isDesktop = useSyncExternalStore(subscribeMediaQuery, getIsDesktop, getIsDesktopServer);

  const closeDrawer = useCallback(() => setOpen(false), []);

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open]);

  // Lock body scroll when drawer is open
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  const toggle = useCallback(() => setOpen((prev) => !prev), []);

  const drawerInert = !isDesktop && !open;

  const nav = (
    <nav className="flex flex-col gap-1">
      <NavLink href="/" label="Dashboard" icon={BarChart3} onClick={closeDrawer} />
      <NavLink href="/scans" label="Scans" icon={ScanSearch} onClick={closeDrawer} />
      <NavLink href="/posts" label="Posts" icon={MessageSquare} onClick={closeDrawer} />
      <NavLink href="/drafts" label="Grading" icon={FileText} onClick={closeDrawer} />
      <NavLink href="/feedback" label="Feedback" icon={ClipboardList} onClick={closeDrawer} />
      <NavLink href="/feedback/overview" label="Feedback Overview" icon={PieChart} onClick={closeDrawer} />
      <NavLink href="/feedback/grades" label="Grade Explorer" icon={ListChecks} onClick={closeDrawer} />
      <NavLink href="/feedback/experiments" label="Experiments" icon={FlaskConical} onClick={closeDrawer} />
      <NavLink href="/traces" label="Traces" icon={Activity} onClick={closeDrawer} />
      <NavLink href="/settings" label="Settings" icon={Settings} onClick={closeDrawer} />
    </nav>
  );

  return (
    <>
      {/* Mobile top bar */}
      <div className="fixed left-0 right-0 top-0 z-50 flex h-14 items-center gap-3 border-b border-gray-200 bg-white px-4 md:hidden dark:border-gray-800 dark:bg-gray-950">
        <button
          type="button"
          onClick={toggle}
          className="rounded-md p-1.5 text-gray-500 hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
          aria-label={open ? "Close menu" : "Open menu"}
          aria-expanded={open}
          aria-controls="mobile-nav"
        >
          {open ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
        </button>
        <h1 className="text-lg font-bold text-gray-900 dark:text-gray-100">Scout</h1>
      </div>

      {/* Mobile backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/60 md:hidden"
          onClick={() => setOpen(false)}
        />
      )}

      {/* Sidebar — always visible on md+, drawer on mobile */}
      <aside
        id="mobile-nav"
        inert={drawerInert || undefined}
        className={`fixed left-0 top-14 z-40 flex h-[calc(100vh-3.5rem)] w-56 flex-col border-r border-gray-200 bg-white px-3 py-4 transition-transform duration-200 ease-in-out md:top-0 md:h-screen md:translate-x-0 dark:border-gray-800 dark:bg-gray-950 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="mb-6 flex items-center justify-between px-3">
          <div>
            <h1 className="text-lg font-bold text-gray-900 dark:text-gray-100">Scout</h1>
            <p className="text-xs text-gray-500 dark:text-gray-400">Scan Dashboard</p>
          </div>
          <ThemeToggle />
        </div>
        {nav}
      </aside>
    </>
  );
}
