"use client";

import { useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { ProjectsTab } from "@/components/organisms/ProjectsTab";
import { KeywordsTab } from "@/components/organisms/KeywordsTab";
import { PromptsTab } from "@/components/organisms/PromptsTab";
import { BlockedAuthorsTab } from "@/components/organisms/BlockedAuthorsTab";

type Tab = "projects" | "keywords" | "prompts" | "blocked-authors";

const TABS: { id: Tab; label: string }[] = [
  { id: "projects", label: "Projects" },
  { id: "keywords", label: "Keywords" },
  { id: "prompts", label: "Prompts" },
  { id: "blocked-authors", label: "Blocked Authors" },
];

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<Tab>("projects");
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  // WAI-ARIA tab pattern: ArrowLeft/Right move (wrapping), Home/End jump to
  // ends, and focus follows selection. Combined with roving tabIndex below.
  const handleTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index;
    switch (e.key) {
      case "ArrowRight":
        next = (index + 1) % TABS.length;
        break;
      case "ArrowLeft":
        next = (index - 1 + TABS.length) % TABS.length;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = TABS.length - 1;
        break;
      default:
        return;
    }
    e.preventDefault();
    setActiveTab(TABS[next].id);
    tabRefs.current[next]?.focus();
  };

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Settings</h1>
        <p className="text-sm text-gray-600 dark:text-gray-500">
          Manage projects, keywords, prompts, and blocked accounts.
        </p>
      </header>

      <div role="tablist" className="flex gap-1 border-b border-gray-200 dark:border-gray-800">
        {TABS.map((tab, index) => (
          <button
            key={tab.id}
            id={`tab-${tab.id}`}
            ref={(el) => {
              tabRefs.current[index] = el;
            }}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            aria-controls={`panel-${tab.id}`}
            tabIndex={activeTab === tab.id ? 0 : -1}
            onClick={() => setActiveTab(tab.id)}
            onKeyDown={(e) => handleTabKeyDown(e, index)}
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === tab.id
                ? "border-b-2 border-blue-400 text-gray-900 dark:text-gray-100"
                : "text-gray-600 hover:text-gray-900 dark:text-gray-500 dark:hover:text-gray-300"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div role="tabpanel" id="panel-projects" aria-labelledby="tab-projects" hidden={activeTab !== "projects"}>
        <ProjectsTab />
      </div>
      <div role="tabpanel" id="panel-keywords" aria-labelledby="tab-keywords" hidden={activeTab !== "keywords"}>
        <KeywordsTab />
      </div>
      <div role="tabpanel" id="panel-prompts" aria-labelledby="tab-prompts" hidden={activeTab !== "prompts"}>
        <PromptsTab />
      </div>
      <div role="tabpanel" id="panel-blocked-authors" aria-labelledby="tab-blocked-authors" hidden={activeTab !== "blocked-authors"}>
        <BlockedAuthorsTab />
      </div>
    </div>
  );
}
