import { describe, it, expect } from "vitest";
import {
  createProjectSchema,
  updateProjectSchema,
  createKeywordSchema,
  createKeywordsSchema,
  updateKeywordSchema,
  keywordMatchTypeEnum,
  createPromptTemplateSchema,
  updatePromptTemplateSchema,
  listFiltersSchema,
  keywordListFiltersSchema,
} from "@/lib/settings-schemas";

describe("createProjectSchema", () => {
  it("accepts valid project", () => {
    const result = createProjectSchema.safeParse({
      key: "my-project",
      name: "My Project",
      description: "A test project",
      link: "https://example.com",
    });
    expect(result.success).toBe(true);
  });

  it("rejects key starting with dash", () => {
    const result = createProjectSchema.safeParse({
      key: "-bad",
      name: "Bad",
      description: "d",
      link: "l",
    });
    expect(result.success).toBe(false);
  });

  it("rejects key with uppercase", () => {
    const result = createProjectSchema.safeParse({
      key: "MyProject",
      name: "n",
      description: "d",
      link: "l",
    });
    expect(result.success).toBe(false);
  });

  it("accepts key with underscores and digits", () => {
    const result = createProjectSchema.safeParse({
      key: "my_project_2",
      name: "n",
      description: "d",
      link: "https://example.com",
    });
    expect(result.success).toBe(true);
  });

  it("trims whitespace from name", () => {
    const result = createProjectSchema.safeParse({
      key: "p",
      name: "  My Project  ",
      description: "d",
      link: "https://example.com",
    });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.name).toBe("My Project");
  });

  it("rejects empty name after trim", () => {
    const result = createProjectSchema.safeParse({
      key: "p",
      name: "   ",
      description: "d",
      link: "https://example.com",
    });
    expect(result.success).toBe(false);
  });

  it("rejects missing required fields", () => {
    const result = createProjectSchema.safeParse({ key: "p" });
    expect(result.success).toBe(false);
  });

  it("rejects a non-URL link", () => {
    const result = createProjectSchema.safeParse({
      key: "p",
      name: "n",
      description: "d",
      link: "not a url",
    });
    expect(result.success).toBe(false);
  });

  it("rejects a javascript: link", () => {
    const result = createProjectSchema.safeParse({
      key: "p",
      name: "n",
      description: "d",
      link: "javascript:alert(1)",
    });
    expect(result.success).toBe(false);
  });

  it("accepts an http link", () => {
    const result = createProjectSchema.safeParse({
      key: "p",
      name: "n",
      description: "d",
      link: "http://example.com/path",
    });
    expect(result.success).toBe(true);
  });

  it("accepts a project without a link", () => {
    const result = createProjectSchema.safeParse({
      key: "p",
      name: "n",
      description: "d",
    });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.link).toBe("");
  });
});

describe("updateProjectSchema", () => {
  it("accepts partial update with just name", () => {
    const result = updateProjectSchema.safeParse({ name: "New Name" });
    expect(result.success).toBe(true);
  });

  it("accepts empty object (no-op update)", () => {
    const result = updateProjectSchema.safeParse({});
    expect(result.success).toBe(true);
  });

  it("rejects empty string name", () => {
    const result = updateProjectSchema.safeParse({ name: "" });
    expect(result.success).toBe(false);
  });

  it("accepts a valid http(s) link", () => {
    const result = updateProjectSchema.safeParse({ link: "https://example.com" });
    expect(result.success).toBe(true);
  });

  it("accepts clearing a link", () => {
    const result = updateProjectSchema.safeParse({ link: "" });
    expect(result.success).toBe(true);
  });

  it("rejects a javascript: link", () => {
    const result = updateProjectSchema.safeParse({ link: "javascript:alert(1)" });
    expect(result.success).toBe(false);
  });
});

describe("createKeywordSchema", () => {
  it("accepts minimal valid keyword", () => {
    const result = createKeywordSchema.safeParse({
      project_key: "myproject",
      keyword: "TypeScript",
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.priority).toBe(100);
      expect(result.data.match_type).toBe("substring");
      expect(result.data.positive_context).toEqual([]);
      expect(result.data.evaluate_prompt).toBeNull();
    }
  });

  it("accepts keyword with routing metadata", () => {
    const result = createKeywordSchema.safeParse({
      project_key: "p",
      keyword: "k",
      match_type: "regex",
      intent: "Reduce spam",
      positive_context: ["trusted user", "clear intent"],
      negative_context: ["ignore this"],
      notes: "internal note",
      evaluate_prompt: "eval_prompt",
      respond_prompt: "respond-prompt",
      critique_prompt: "critique1",
      priority: 50,
    });
    expect(result.success).toBe(true);
  });

  it("rejects invalid match_type", () => {
    const result = createKeywordSchema.safeParse({
      project_key: "p",
      keyword: "k",
      match_type: "prefix",
    });
    expect(result.success).toBe(false);
  });

  it("rejects empty context entries", () => {
    const result = createKeywordSchema.safeParse({
      project_key: "p",
      keyword: "k",
      positive_context: ["good", "   "],
    });
    expect(result.success).toBe(false);
  });

  it("rejects overly long intent", () => {
    const result = createKeywordSchema.safeParse({
      project_key: "p",
      keyword: "k",
      intent: "x".repeat(281),
    });
    expect(result.success).toBe(false);
  });

  it("rejects negative priority", () => {
    const result = createKeywordSchema.safeParse({
      project_key: "p",
      keyword: "k",
      priority: -1,
    });
    expect(result.success).toBe(false);
  });

  it("rejects prompt name with invalid pattern", () => {
    const result = createKeywordSchema.safeParse({
      project_key: "p",
      keyword: "k",
      evaluate_prompt: "-bad_name",
    });
    expect(result.success).toBe(false);
  });

  it("accepts null prompt names explicitly", () => {
    const result = createKeywordSchema.safeParse({
      project_key: "p",
      keyword: "k",
      evaluate_prompt: null,
    });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.evaluate_prompt).toBeNull();
  });

  it("trims keyword", () => {
    const result = createKeywordSchema.safeParse({
      project_key: "p",
      keyword: "  rust  ",
    });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.keyword).toBe("rust");
  });

  it("rejects whitespace-only keyword", () => {
    const result = createKeywordSchema.safeParse({
      project_key: "p",
      keyword: "   ",
    });
    expect(result.success).toBe(false);
  });
});

describe("createKeywordsSchema", () => {
  it("accepts multiple trimmed keywords", () => {
    const result = createKeywordsSchema.safeParse({
      project_key: "p",
      keywords: ["  react  ", "typescript"],
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.keywords).toEqual(["react", "typescript"]);
      expect(result.data.match_type).toBe("substring");
    }
  });

  it("rejects an empty keyword list", () => {
    const result = createKeywordsSchema.safeParse({
      project_key: "p",
      keywords: [],
    });
    expect(result.success).toBe(false);
  });

  it("rejects blank keyword entries", () => {
    const result = createKeywordsSchema.safeParse({
      project_key: "p",
      keywords: ["react", "   "],
    });
    expect(result.success).toBe(false);
  });
});

describe("keywordMatchTypeEnum", () => {
  it("only accepts supported match types", () => {
    for (const value of ["substring", "phrase", "exact", "regex"]) {
      expect(keywordMatchTypeEnum.safeParse(value).success).toBe(true);
    }
    expect(keywordMatchTypeEnum.safeParse("prefix").success).toBe(false);
  });
});

describe("updateKeywordSchema", () => {
  it("accepts partial metadata updates", () => {
    const result = updateKeywordSchema.safeParse({
      match_type: "exact",
      intent: "New intent",
      positive_context: ["a", "b"],
      notes: null,
    });
    expect(result.success).toBe(true);
  });

  it("rejects invalid enum values", () => {
    const result = updateKeywordSchema.safeParse({
      match_type: "prefix",
    });
    expect(result.success).toBe(false);
  });

  it("rejects empty note text", () => {
    const result = updateKeywordSchema.safeParse({
      notes: "   ",
    });
    expect(result.success).toBe(false);
  });
});

describe("createPromptTemplateSchema", () => {
  it("accepts valid prompt", () => {
    const result = createPromptTemplateSchema.safeParse({
      name: "my_eval",
      body: "Evaluate this: {{content}}",
      kind: "evaluate",
    });
    expect(result.success).toBe(true);
  });

  it("accepts all valid kinds", () => {
    for (const kind of [
      "evaluate",
      "respond",
      "critique",
      "shared",
      "custom",
    ]) {
      const result = createPromptTemplateSchema.safeParse({
        name: "p",
        body: "b",
        kind,
      });
      expect(result.success).toBe(true);
    }
  });

  it("rejects invalid kind", () => {
    const result = createPromptTemplateSchema.safeParse({
      name: "p",
      body: "b",
      kind: "unknown",
    });
    expect(result.success).toBe(false);
  });

  it("rejects empty body", () => {
    const result = createPromptTemplateSchema.safeParse({
      name: "p",
      body: "",
      kind: "evaluate",
    });
    expect(result.success).toBe(false);
  });

  it("accepts name starting with digit", () => {
    const result = createPromptTemplateSchema.safeParse({
      name: "1prompt",
      body: "b",
      kind: "evaluate",
    });
    expect(result.success).toBe(true);
  });

  it("rejects name starting with dash", () => {
    const result = createPromptTemplateSchema.safeParse({
      name: "-bad",
      body: "b",
      kind: "evaluate",
    });
    expect(result.success).toBe(false);
  });
});

describe("listFiltersSchema", () => {
  it("defaults include_inactive to false", () => {
    const result = listFiltersSchema.safeParse({});
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.include_inactive).toBe(false);
  });

  it("coerces include_inactive=true", () => {
    const result = listFiltersSchema.safeParse({ include_inactive: "true" });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.include_inactive).toBe(true);
  });

  it("rejects invalid include_inactive value", () => {
    const result = listFiltersSchema.safeParse({ include_inactive: "yes" });
    expect(result.success).toBe(false);
  });
});

describe("keywordListFiltersSchema", () => {
  it("accepts project_key filter", () => {
    const result = keywordListFiltersSchema.safeParse({
      project_key: "my-proj",
      include_inactive: "false",
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.project_key).toBe("my-proj");
      expect(result.data.include_inactive).toBe(false);
    }
  });

  it("accepts empty object", () => {
    const result = keywordListFiltersSchema.safeParse({});
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.project_key).toBeUndefined();
  });
});

describe("updateKeywordSchema", () => {
  it("accepts partial update with just priority", () => {
    const result = updateKeywordSchema.safeParse({ priority: 50 });
    expect(result.success).toBe(true);
  });

  it("accepts setting active=false", () => {
    const result = updateKeywordSchema.safeParse({ active: false });
    expect(result.success).toBe(true);
  });
});

describe("updatePromptTemplateSchema", () => {
  it("accepts partial update with just body", () => {
    const result = updatePromptTemplateSchema.safeParse({ body: "new body" });
    expect(result.success).toBe(true);
  });

  it("rejects empty body", () => {
    const result = updatePromptTemplateSchema.safeParse({ body: "" });
    expect(result.success).toBe(false);
  });

  it("rejects invalid kind", () => {
    const result = updatePromptTemplateSchema.safeParse({ kind: "bad" });
    expect(result.success).toBe(false);
  });
});
