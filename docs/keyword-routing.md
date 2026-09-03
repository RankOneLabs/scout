# Keyword Routing and Prompt Overrides

Scout routes inbound posts to projects through keyword rows. A project does not
select prompts directly. Instead:

1. A project defines the target Scout may recommend.
2. A keyword belongs to one project.
3. A matched keyword route points Scout at that project.
4. Optional prompt overrides on the keyword replace the scan defaults for that
   one route.

If a keyword does not set prompt overrides, the scan mode's default evaluate,
respond, and critique prompts are used.

## Keyword Form Fields

### Project

The project this keyword routes to. When a message matches this keyword, Scout
uses this project as the candidate target and includes the project's name,
description, and link in the agent prompt.

### Keyword

The text or pattern used by the prefilter. This is the first routing gate before
the LLM sees a post.

When creating new keywords in the web UI, this field accepts CSV-style
comma-separated values. Each value creates a separate keyword row with the same
project, match type, priority, intent, context, notes, and prompt overrides.
Editing an existing keyword still edits only that one row.

Examples:

- `postgres`
- `agent framework`
- `postgres, vector database, RAG`
- `\bRAG\b` when using regex matching

### Match Type

Controls how the keyword is matched against inbound message text.

- `Substring`: matches when the keyword appears anywhere in the text. This is
  the broadest and default option.
- `Phrase`: case-insensitive phrase match after whitespace is normalized.
- `Exact`: case-insensitive token match. The keyword must normalize to one
  token; multi-token exact routes are disabled.
- `Regex`: treats the keyword field as a case-insensitive regular expression.

Use broader matching for recall and narrower matching when a term creates too
many false positives.

### Priority

Controls route precedence. Lower numbers run first. If multiple keywords could
match the same message, the first matching route wins based on priority, keyword
length, and row id ordering.

Use this when a specific route should beat a broader one.

Example: give `postgres migration tooling` a lower priority number than
`postgres`.

### Intent

Short guidance for what this keyword is meant to catch. Intent is not a prompt
template. It is route metadata attached to the matched message so the agent can
interpret the match.

Good intent examples:

- `Find teams asking for help choosing a vector database migration path.`
- `Catch posts where developers are frustrated with flaky CI and may need build
  observability.`

Leave it blank if the keyword is self-explanatory.

### Positive Context

One phrase per line describing surrounding signals that make a keyword match
more likely to be relevant. These are passed to the agent with the matched route.

Examples:

```text
asking for recommendations
mentions production incident
evaluating alternatives
complains about manual workflow
```

Positive context helps the agent distinguish useful opportunities from generic
mentions.

### Negative Context

One phrase per line describing signals that should count against relevance.
These are also passed to the agent with the matched route.

Examples:

```text
hiring post
job description
news article with no question
unrelated product announcement
```

Use negative context for common false positives.

### Notes

Internal operator notes. These are for humans managing the route. They are stored
with the keyword record but are not part of the agent-facing route context.

### Active

Controls whether the keyword route participates in scans. Inactive keywords stay
in the database but are not loaded into the runtime registry.

## Advanced Prompt Overrides

Prompt overrides are optional per-keyword links to prompt templates. They do not
belong directly to the project.

### Evaluate Prompt

Overrides the default relevance evaluation prompt for messages matched by this
keyword route.

Use this when a route needs a different relevance rubric than the scan default.

### Respond Prompt

Overrides the default drafting prompt for messages matched by this keyword
route.

Use this when comments for this route need a different style, angle, or response
shape.

### Critique Prompt

Overrides the default self-critique prompt for messages matched by this keyword
route.

Use this when drafts for this route should be checked against a specific risk or
quality bar.

## Runtime Flow

At scan time, Scout loads active projects, active keywords, and active prompt
templates from SQLite.

For each inbound message:

1. The prefilter checks the active keyword routes.
2. The winning keyword route supplies `project_key`, keyword metadata, and any
   prompt override names.
3. Scout resolves the prompt bundle:
   - use the keyword's override prompt when present;
   - otherwise use the scan mode default prompt.
4. The agent receives the project list plus the matched route metadata:
   keyword, intent, positive context, and negative context.
5. The agent decides relevance, drafts a reply when relevant, critiques it, and
   submits the final candidate.

In short: projects define what Scout can recommend; keywords define when a
project is relevant; prompt overrides customize how that specific route is
evaluated and answered.
