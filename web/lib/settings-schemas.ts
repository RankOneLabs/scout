import { z } from "zod";

const PROJECT_KEY_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;
const PROMPT_NAME_PATTERN = /^[a-zA-Z0-9_][a-zA-Z0-9_-]*$/;

export const projectKeySchema = z
  .string()
  .regex(
    PROJECT_KEY_PATTERN,
    "must start with a letter/digit and contain only lowercase letters, digits, underscores, or hyphens"
  );

const nullablePromptName = z
  .string()
  .regex(
    PROMPT_NAME_PATTERN,
    "must start with a letter, digit, or underscore, and contain only letters, digits, underscores, or hyphens"
  )
  .nullable()
  .optional()
  .default(null);

// For update schemas: omitted fields stay undefined (no default) so the query
// layer can distinguish "not sent" from "explicitly set to null".
const nullablePromptNameUpdate = z
  .string()
  .regex(
    PROMPT_NAME_PATTERN,
    "must start with a letter, digit, or underscore, and contain only letters, digits, underscores, or hyphens"
  )
  .nullable()
  .optional();

export const promptKindEnum = z.enum([
  "evaluate",
  "respond",
  "critique",
  "shared",
  "custom",
]);

export const keywordMatchTypeEnum = z.enum([
  "substring",
  "phrase",
  "exact",
  "regex",
]);

const boundedText = (max: number, label: string) =>
  z
    .string()
    .trim()
    .min(1, `${label} is required`)
    .max(max, `${label} must be at most ${max} characters`);

const nullableBoundedText = (max: number, label: string) =>
  boundedText(max, label).nullable().optional().default(null);

const nullableBoundedTextUpdate = (max: number, label: string) =>
  boundedText(max, label).nullable().optional();

const keywordContextEntrySchema = boundedText(160, "context entry");
const keywordContextArraySchema = z.array(keywordContextEntrySchema);

// `link` is rendered into an `<a href>` (via ExternalLink), so it must be a
// valid http(s) URL — a non-empty string check alone would let unsafe schemes
// like `javascript:` be stored and served to the UI.
const projectLink = z
  .string()
  .trim()
  .refine((value) => {
    if (value === "") return true;
    let url: URL;
    try {
      url = new URL(value);
    } catch {
      return false;
    }
    return url.protocol === "http:" || url.protocol === "https:";
  }, "link must be a valid http(s) URL");

export const createProjectSchema = z.object({
  key: projectKeySchema,
  name: z.string().trim().min(1, "name is required"),
  description: z.string().trim().min(1, "description is required"),
  link: projectLink.optional().default(""),
  dossier_summary_id: z.string().trim().min(1).nullable().optional().default(null),
});

export const updateProjectSchema = z.object({
  name: z.string().trim().min(1, "name is required").optional(),
  description: z.string().trim().min(1, "description is required").optional(),
  link: projectLink.optional(),
  dossier_summary_id: z.string().trim().min(1).nullable().optional(),
});

export const patchActiveSchema = z.object({
  active: z.boolean(),
});

export const blockAuthorSchema = z.object({
  platform: boundedText(40, "platform").transform((value) => value.toLowerCase()),
  author_id: boundedText(300, "author_id"),
  author_name: nullableBoundedText(300, "author_name"),
  reason: nullableBoundedText(500, "reason"),
});

export const createKeywordSchema = z.object({
  project_key: projectKeySchema,
  keyword: z.string().trim().min(1, "keyword is required"),
  match_type: keywordMatchTypeEnum.optional().default("substring"),
  intent: nullableBoundedText(280, "intent"),
  positive_context: keywordContextArraySchema.optional().default([]),
  negative_context: keywordContextArraySchema.optional().default([]),
  notes: nullableBoundedText(500, "notes"),
  evaluate_prompt: nullablePromptName,
  respond_prompt: nullablePromptName,
  critique_prompt: nullablePromptName,
  priority: z.number().int().nonnegative().default(100),
  active: z.boolean().optional().default(true),
});

export const createKeywordsSchema = z.object({
  project_key: projectKeySchema,
  keywords: z
    .array(z.string().trim().min(1, "keyword is required"))
    .min(1, "at least one keyword is required"),
  match_type: keywordMatchTypeEnum.optional().default("substring"),
  intent: nullableBoundedText(280, "intent"),
  positive_context: keywordContextArraySchema.optional().default([]),
  negative_context: keywordContextArraySchema.optional().default([]),
  notes: nullableBoundedText(500, "notes"),
  evaluate_prompt: nullablePromptName,
  respond_prompt: nullablePromptName,
  critique_prompt: nullablePromptName,
  priority: z.number().int().nonnegative().default(100),
  active: z.boolean().optional().default(true),
});

export const updateKeywordSchema = z.object({
  project_key: projectKeySchema.optional(),
  keyword: z.string().trim().min(1, "keyword is required").optional(),
  match_type: keywordMatchTypeEnum.optional(),
  intent: nullableBoundedTextUpdate(280, "intent"),
  positive_context: keywordContextArraySchema.optional(),
  negative_context: keywordContextArraySchema.optional(),
  notes: nullableBoundedTextUpdate(500, "notes"),
  evaluate_prompt: nullablePromptNameUpdate,
  respond_prompt: nullablePromptNameUpdate,
  critique_prompt: nullablePromptNameUpdate,
  priority: z.number().int().nonnegative().optional(),
  active: z.boolean().optional(),
});

export const createPromptTemplateSchema = z.object({
  name: z
    .string()
    .regex(
      PROMPT_NAME_PATTERN,
      "must start with a letter, digit, or underscore, and contain only letters, digits, underscores, or hyphens"
    ),
  body: z.string().min(1, "body is required"),
  kind: promptKindEnum,
});

export const updatePromptTemplateSchema = z.object({
  body: z.string().min(1, "body is required").optional(),
  kind: promptKindEnum.optional(),
});

export const listFiltersSchema = z.object({
  include_inactive: z
    .enum(["true", "false"])
    .optional()
    .transform((v) => v === "true"),
});

export const keywordListFiltersSchema = listFiltersSchema.extend({
  project_key: z.string().min(1).optional(),
});

export type CreateProjectInput = z.infer<typeof createProjectSchema>;
export type UpdateProjectInput = z.infer<typeof updateProjectSchema>;
export type PatchActiveInput = z.infer<typeof patchActiveSchema>;
export type BlockAuthorInput = z.infer<typeof blockAuthorSchema>;
export type CreateKeywordInput = z.infer<typeof createKeywordSchema>;
export type CreateKeywordsInput = z.infer<typeof createKeywordsSchema>;
export type UpdateKeywordInput = z.infer<typeof updateKeywordSchema>;
export type CreatePromptTemplateInput = z.infer<typeof createPromptTemplateSchema>;
export type UpdatePromptTemplateInput = z.infer<typeof updatePromptTemplateSchema>;
export type ListFiltersInput = z.infer<typeof listFiltersSchema>;
export type KeywordListFiltersInput = z.infer<typeof keywordListFiltersSchema>;
