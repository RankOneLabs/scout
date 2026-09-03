// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useSettingsProjects } from "@/hooks/use-settings-projects";
import { useSettingsKeywords } from "@/hooks/use-settings-keywords";
import { useSettingsPrompts } from "@/hooks/use-settings-prompts";
import type {
  ProjectListItem,
  ProjectKeywordRow,
  PromptTemplateListItem,
} from "@/types/schema";

// ─── fixtures ─────────────────────────────────────────────────────────────────

function makeProject(overrides?: Partial<ProjectListItem>): ProjectListItem {
  return {
    key: "test-project",
    name: "Test Project",
    description: "A test project",
    link: "https://example.com",
    active: true,
    keyword_count: 0,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    ...overrides,
  };
}

function makeKeyword(overrides?: Partial<ProjectKeywordRow>): ProjectKeywordRow {
  return {
    id: 1,
    project_key: "test-project",
    keyword: "test keyword",
    match_type: "substring",
    intent: null,
    positive_context: [],
    negative_context: [],
    notes: null,
    evaluate_prompt: null,
    respond_prompt: null,
    critique_prompt: null,
    active: true,
    priority: 100,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    ...overrides,
  };
}

function makePrompt(
  overrides?: Partial<PromptTemplateListItem>
): PromptTemplateListItem {
  return {
    name: "my_prompt",
    body: "You are a helpful assistant.",
    kind: "evaluate",
    active: true,
    referenced_keyword_count: 0,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    ...overrides,
  };
}

function okResponse(data: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => data,
  } as Response);
}

function errResponse(status = 500) {
  return Promise.resolve({
    ok: false,
    status,
    json: async () => ({}),
  } as Response);
}

// ─── useSettingsProjects ──────────────────────────────────────────────────────

describe("useSettingsProjects", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("fetches projects on mount and exposes them", async () => {
    const project = makeProject();
    vi.mocked(global.fetch).mockReturnValueOnce(
      okResponse({ projects: [project] })
    );

    const { result } = renderHook(() => useSettingsProjects());
    expect(result.current.loading).toBe(true);

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.projects).toHaveLength(1);
    expect(result.current.projects[0].key).toBe("test-project");
    expect(result.current.error).toBeNull();
    expect(vi.mocked(global.fetch)).toHaveBeenCalledWith("/api/settings/projects?include_inactive=true");
  });

  it("surfaces an error when the fetch fails", async () => {
    vi.mocked(global.fetch).mockReturnValueOnce(errResponse(503));

    const { result } = renderHook(() => useSettingsProjects());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toMatch(/503/);
    expect(result.current.projects).toHaveLength(0);
  });

  it("refresh() re-fetches and updates the list", async () => {
    const fetchMock = vi.mocked(global.fetch);
    fetchMock
      .mockReturnValueOnce(okResponse({ projects: [] }))
      .mockReturnValueOnce(okResponse({ projects: [makeProject()] }));

    const { result } = renderHook(() => useSettingsProjects());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.projects).toHaveLength(0);

    await act(async () => {
      result.current.refresh();
    });

    await waitFor(() => expect(result.current.projects).toHaveLength(1));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

// ─── useSettingsKeywords ──────────────────────────────────────────────────────

describe("useSettingsKeywords", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("fetches keywords on mount without a project filter", async () => {
    const kw = makeKeyword();
    vi.mocked(global.fetch).mockReturnValueOnce(
      okResponse({ keywords: [kw] })
    );

    const { result } = renderHook(() => useSettingsKeywords());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.keywords).toHaveLength(1);
    expect(result.current.keywords[0].keyword).toBe("test keyword");
    expect(vi.mocked(global.fetch)).toHaveBeenCalledWith("/api/settings/keywords?include_inactive=true");
  });

  it("appends project_key filter to the URL when provided", async () => {
    vi.mocked(global.fetch).mockReturnValueOnce(
      okResponse({ keywords: [] })
    );

    const { result } = renderHook(() => useSettingsKeywords("my-project"));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(vi.mocked(global.fetch)).toHaveBeenCalledWith(
      "/api/settings/keywords?project_key=my-project&include_inactive=true"
    );
  });

  it("refresh() re-fetches and updates the list", async () => {
    const fetchMock = vi.mocked(global.fetch);
    fetchMock
      .mockReturnValueOnce(okResponse({ keywords: [] }))
      .mockReturnValueOnce(okResponse({ keywords: [makeKeyword()] }));

    const { result } = renderHook(() => useSettingsKeywords());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.keywords).toHaveLength(0);

    await act(async () => {
      result.current.refresh();
    });

    await waitFor(() => expect(result.current.keywords).toHaveLength(1));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

// ─── useSettingsPrompts ───────────────────────────────────────────────────────

describe("useSettingsPrompts", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("fetches prompts on mount and exposes them", async () => {
    const prompt = makePrompt();
    vi.mocked(global.fetch).mockReturnValueOnce(
      okResponse({ prompts: [prompt] })
    );

    const { result } = renderHook(() => useSettingsPrompts());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.prompts).toHaveLength(1);
    expect(result.current.prompts[0].name).toBe("my_prompt");
    expect(vi.mocked(global.fetch)).toHaveBeenCalledWith("/api/settings/prompts?include_inactive=true");
  });

  it("surfaces an error when the fetch fails", async () => {
    vi.mocked(global.fetch).mockReturnValueOnce(errResponse(500));

    const { result } = renderHook(() => useSettingsPrompts());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toMatch(/500/);
    expect(result.current.prompts).toHaveLength(0);
  });

  it("refresh() re-fetches and updates the list", async () => {
    const fetchMock = vi.mocked(global.fetch);
    fetchMock
      .mockReturnValueOnce(okResponse({ prompts: [] }))
      .mockReturnValueOnce(okResponse({ prompts: [makePrompt()] }));

    const { result } = renderHook(() => useSettingsPrompts());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.prompts).toHaveLength(0);

    await act(async () => {
      result.current.refresh();
    });

    await waitFor(() => expect(result.current.prompts).toHaveLength(1));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
