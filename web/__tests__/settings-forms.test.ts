// @vitest-environment jsdom

import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { ProjectForm } from "@/components/organisms/ProjectForm";
import { KeywordForm } from "@/components/organisms/KeywordForm";
import { PromptForm } from "@/components/organisms/PromptForm";
import type {
  ProjectRow,
  ProjectKeywordRow,
  PromptTemplateRow,
  PromptTemplateListItem,
} from "@/types/schema";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// ─── fixtures ─────────────────────────────────────────────────────────────────

function makeProject(overrides?: Partial<ProjectRow>): ProjectRow {
  return {
    key: "my-project",
    name: "My Project",
    description: "A project",
    link: "https://example.com",
    active: true,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    ...overrides,
  };
}

function makeKeyword(overrides?: Partial<ProjectKeywordRow>): ProjectKeywordRow {
  return {
    id: 7,
    project_key: "my-project",
    keyword: "rust async",
    match_type: "substring",
    intent: null,
    positive_context: [],
    negative_context: [],
    notes: null,
    evaluate_prompt: null,
    respond_prompt: null,
    critique_prompt: null,
    active: true,
    priority: 50,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    ...overrides,
  };
}

function makePromptRow(overrides?: Partial<PromptTemplateRow>): PromptTemplateRow {
  return {
    name: "eval_default",
    body: "Evaluate this post.",
    kind: "evaluate",
    active: true,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    ...overrides,
  };
}

function okResponse(data: unknown = {}, status = 200) {
  return Promise.resolve({
    ok: true,
    status,
    json: async () => data,
  } as Response);
}

function errResponse(data: unknown = {}, status = 400) {
  return Promise.resolve({
    ok: false,
    status,
    json: async () => data,
  } as Response);
}

// ─── ProjectForm ──────────────────────────────────────────────────────────────

describe("ProjectForm — create", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

  it("POSTs to /api/settings/projects with key/name/description/link", async () => {
    const onDone = vi.fn();
    vi.mocked(global.fetch).mockReturnValueOnce(okResponse({}, 201));

    const { getByPlaceholderText, getByRole } = render(
      React.createElement(ProjectForm, { onDone })
    );

    fireEvent.change(getByPlaceholderText("my-project"), {
      target: { value: "new-proj" },
    });
    fireEvent.change(getByRole("textbox", { name: /name/i }), {
      target: { value: "New Proj" },
    });
    fireEvent.change(getByRole("textbox", { name: /link/i }), {
      target: { value: "https://new.example.com" },
    });
    fireEvent.change(getByRole("textbox", { name: /description/i }), {
      target: { value: "A new project description" },
    });

    fireEvent.click(getByRole("button", { name: "Create" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));

    expect(vi.mocked(global.fetch)).toHaveBeenCalledWith(
      "/api/settings/projects",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining('"key":"new-proj"'),
      })
    );
    const body = JSON.parse(
      (vi.mocked(global.fetch).mock.calls[0][1] as RequestInit).body as string
    );
    expect(body).toEqual({
      key: "new-proj",
      name: "New Proj",
      description: "A new project description",
      link: "https://new.example.com",
      dossier_summary_id: null,
    });
  });
});

describe("ProjectForm — edit", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

  it("PUTs to /api/settings/projects/[key] with name/description/link", async () => {
    const onDone = vi.fn();
    vi.mocked(global.fetch).mockReturnValueOnce(okResponse());

    const project = makeProject();
    const { getByRole } = render(
      React.createElement(ProjectForm, { initial: project, onDone })
    );

    fireEvent.click(getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));

    const calls = vi.mocked(global.fetch).mock.calls;
    expect(calls[0][0]).toBe("/api/settings/projects/my-project");
    expect((calls[0][1] as RequestInit).method).toBe("PUT");
    const body = JSON.parse((calls[0][1] as RequestInit).body as string);
    expect(body).toMatchObject({ name: "My Project", link: "https://example.com" });
  });

  it("PATCHes active when the checkbox is toggled", async () => {
    const onDone = vi.fn();
    vi.mocked(global.fetch)
      .mockReturnValueOnce(okResponse()) // PUT
      .mockReturnValueOnce(okResponse()); // PATCH active

    const project = makeProject({ active: true });
    const { getByRole } = render(
      React.createElement(ProjectForm, { initial: project, onDone })
    );

    fireEvent.click(getByRole("checkbox"));
    fireEvent.click(getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));

    const calls = vi.mocked(global.fetch).mock.calls;
    expect(calls).toHaveLength(2);
    expect((calls[1][1] as RequestInit).method).toBe("PATCH");
    const patchBody = JSON.parse((calls[1][1] as RequestInit).body as string);
    expect(patchBody).toEqual({ active: false });
  });

  it("allows pausing a project that has no link", async () => {
    const onDone = vi.fn();
    vi.mocked(global.fetch)
      .mockReturnValueOnce(okResponse())
      .mockReturnValueOnce(okResponse());

    const project = makeProject({ active: true, link: "" });
    const { getByRole } = render(
      React.createElement(ProjectForm, { initial: project, onDone })
    );

    fireEvent.click(getByRole("checkbox"));
    fireEvent.click(getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    const calls = vi.mocked(global.fetch).mock.calls;
    expect(calls).toHaveLength(2);
    expect(JSON.parse((calls[0][1] as RequestInit).body as string).link).toBe("");
    expect(JSON.parse((calls[1][1] as RequestInit).body as string)).toEqual({
      active: false,
    });
  });
});

// ─── KeywordForm ──────────────────────────────────────────────────────────────

describe("KeywordForm — create", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

  it("POSTs to /api/settings/keywords with correct shape", async () => {
    const onDone = vi.fn();
    vi.mocked(global.fetch).mockReturnValueOnce(okResponse({}, 201));

    const projects = [makeProject()];
    const prompts: PromptTemplateListItem[] = [];

    const { getByRole } = render(
      React.createElement(KeywordForm, {
        projects,
        prompts,
        defaultProjectKey: "my-project",
        onDone,
      })
    );

    fireEvent.change(getByRole("textbox", { name: /keyword/i }), {
      target: { value: "distributed systems" },
    });

    fireEvent.click(getByRole("button", { name: "Create" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));

    const calls = vi.mocked(global.fetch).mock.calls;
    expect(calls[0][0]).toBe("/api/settings/keywords");
    expect((calls[0][1] as RequestInit).method).toBe("POST");
    const body = JSON.parse((calls[0][1] as RequestInit).body as string);
    expect(body.project_key).toBe("my-project");
    expect(body.keywords).toEqual(["distributed systems"]);
    expect(body.match_type).toBe("substring");
    expect(body.intent).toBeNull();
    expect(body.positive_context).toEqual([]);
    expect(body.negative_context).toEqual([]);
    expect(body.notes).toBeNull();
    expect(body.priority).toBe(100);
    expect(body.evaluate_prompt).toBeNull();
    expect(body.respond_prompt).toBeNull();
    expect(body.critique_prompt).toBeNull();
    expect(body.active).toBe(true);
  });

  it("POSTs comma-separated keywords as one bulk request", async () => {
    const onDone = vi.fn();
    vi.mocked(global.fetch).mockReturnValueOnce(okResponse({ ids: [1, 2, 3] }, 201));

    const { getByRole } = render(
      React.createElement(KeywordForm, {
        projects: [makeProject()],
        prompts: [],
        defaultProjectKey: "my-project",
        onDone,
      })
    );

    fireEvent.change(getByRole("textbox", { name: /keyword/i }), {
      target: { value: "react, typescript, next.js" },
    });
    fireEvent.click(getByRole("button", { name: "Create" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));

    const calls = vi.mocked(global.fetch).mock.calls;
    expect(calls).toHaveLength(1);
    expect(calls[0][0]).toBe("/api/settings/keywords");
    const body = JSON.parse((calls[0][1] as RequestInit).body as string);
    expect(body.keywords).toEqual(["react", "typescript", "next.js"]);
  });

  it("shows a local error when project is blank", async () => {
    const onDone = vi.fn();

    const { getByRole, getByText } = render(
      React.createElement(KeywordForm, {
        projects: [makeProject()],
        prompts: [],
        onDone,
      })
    );

    fireEvent.change(getByRole("textbox", { name: /keyword/i }), {
      target: { value: "distributed systems" },
    });
    fireEvent.click(getByRole("button", { name: "Create" }));

    expect(getByText("Project is required.")).toBeTruthy();
    expect(global.fetch).not.toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("shows a local error when keyword is blank", async () => {
    const onDone = vi.fn();

    const { getByRole, getByText } = render(
      React.createElement(KeywordForm, {
        projects: [makeProject()],
        prompts: [],
        defaultProjectKey: "my-project",
        onDone,
      })
    );

    fireEvent.change(getByRole("textbox", { name: /keyword/i }), {
      target: { value: "   " },
    });
    fireEvent.click(getByRole("button", { name: "Create" }));

    expect(getByText("Keyword is required.")).toBeTruthy();
    expect(global.fetch).not.toHaveBeenCalled();
    expect(onDone).not.toHaveBeenCalled();
  });
});

describe("KeywordForm — edit", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

  it("PUTs metadata to /api/settings/keywords/[id] and calls onDone", async () => {
    const onDone = vi.fn();
    vi.mocked(global.fetch).mockReturnValueOnce(okResponse());

    const kw = makeKeyword();
    const { getByRole } = render(
      React.createElement(KeywordForm, {
        initial: kw,
        projects: [makeProject()],
        prompts: [],
        onDone,
      })
    );

    fireEvent.click(getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));

    const calls = vi.mocked(global.fetch).mock.calls;
    expect(calls[0][0]).toBe("/api/settings/keywords/7");
    expect((calls[0][1] as RequestInit).method).toBe("PUT");
    const body = JSON.parse((calls[0][1] as RequestInit).body as string);
    expect(body.keyword).toBe("rust async");
    expect(body.project_key).toBe("my-project");
    expect(body.match_type).toBe("substring");
    expect(body.intent).toBeNull();
    expect(body.positive_context).toEqual([]);
    expect(body.negative_context).toEqual([]);
    expect(body.notes).toBeNull();
    expect(body.evaluate_prompt).toBeNull();
    expect(body.respond_prompt).toBeNull();
    expect(body.critique_prompt).toBeNull();
    expect(body.active).toBe(true);
  });

  it("sends active in the same PUT when toggled", async () => {
    const onDone = vi.fn();
    vi.mocked(global.fetch).mockReturnValueOnce(okResponse());

    const kw = makeKeyword({ active: true });
    const { getByRole } = render(
      React.createElement(KeywordForm, {
        initial: kw,
        projects: [makeProject()],
        prompts: [],
        onDone,
      })
    );

    fireEvent.click(getByRole("checkbox"));
    fireEvent.click(getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));

    const calls = vi.mocked(global.fetch).mock.calls;
    expect(calls).toHaveLength(1);
    expect((calls[0][1] as RequestInit).method).toBe("PUT");
    const body = JSON.parse((calls[0][1] as RequestInit).body as string);
    expect(body.active).toBe(false);
  });
});

// ─── PromptForm ───────────────────────────────────────────────────────────────

describe("PromptForm — create", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

  it("POSTs to /api/settings/prompts with name/body/kind", async () => {
    const onDone = vi.fn();
    vi.mocked(global.fetch).mockReturnValueOnce(okResponse({}, 201));

    const { getByPlaceholderText, getByRole } = render(
      React.createElement(PromptForm, { onDone })
    );

    fireEvent.change(getByPlaceholderText("my_prompt"), {
      target: { value: "eval_v2" },
    });
    fireEvent.change(getByRole("textbox", { name: /body/i }), {
      target: { value: "Score this post." },
    });

    fireEvent.click(getByRole("button", { name: "Create" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));

    const calls = vi.mocked(global.fetch).mock.calls;
    expect(calls[0][0]).toBe("/api/settings/prompts");
    expect((calls[0][1] as RequestInit).method).toBe("POST");
    const body = JSON.parse((calls[0][1] as RequestInit).body as string);
    expect(body).toEqual({ name: "eval_v2", body: "Score this post.", kind: "evaluate" });
  });
});

describe("PromptForm — edit", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

  it("PUTs body/kind and calls onDone", async () => {
    const onDone = vi.fn();
    vi.mocked(global.fetch).mockReturnValueOnce(okResponse());

    const prompt = makePromptRow();
    const { getByRole } = render(
      React.createElement(PromptForm, { initial: prompt, onDone })
    );

    fireEvent.click(getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));

    const calls = vi.mocked(global.fetch).mock.calls;
    expect(calls[0][0]).toBe("/api/settings/prompts/eval_default");
    expect((calls[0][1] as RequestInit).method).toBe("PUT");
  });

  it("shows conflict message on 409 and retries with confirm=true", async () => {
    const onDone = vi.fn();
    vi.mocked(global.fetch)
      .mockReturnValueOnce(okResponse()) // PUT succeeds
      .mockReturnValueOnce(
        errResponse({ error: "referenced by 2 active keywords" }, 409)
      ) // PATCH 409
      .mockReturnValueOnce(okResponse()) // PUT again (retry Save)
      .mockReturnValueOnce(okResponse()); // PATCH confirm=true

    const prompt = makePromptRow({ active: true });
    const { getByRole, findByText } = render(
      React.createElement(PromptForm, { initial: prompt, onDone })
    );

    // toggle active off
    fireEvent.click(getByRole("checkbox"));
    fireEvent.click(getByRole("button", { name: "Save" }));

    const conflictMsg = await findByText(/referenced by 2 active keywords/i);
    expect(conflictMsg).toBeTruthy();

    const forceBtn = getByRole("button", { name: /force deactivate/i });
    fireEvent.click(forceBtn);

    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));

    const calls = vi.mocked(global.fetch).mock.calls;
    const lastPatchBody = JSON.parse(
      (calls[calls.length - 1][1] as RequestInit).body as string
    );
    expect(lastPatchBody).toEqual({ active: false, confirm: true });
  });
});
