import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";

// C-002 (docs/verification-e3-content-works.md) breadth check: the report
// covers review, publication, grading, and "other writes" generally, not
// just the grade/scans routes sampled directly in grade-routes.test.ts and
// scans-routes.test.ts. Rather than hand-picking a few more routes to
// sample, this walks every route.ts under app/api and asserts that any
// file exporting a mutating HTTP method (POST/PUT/PATCH/DELETE) also
// imports and calls isTrustedWriteContext — the single enforcement point
// write-guard.test.ts characterizes in isolation. A route that mutates
// without referencing the guard would be an unguarded browser write path
// and should fail this test.

const API_ROOT = path.resolve(__dirname, "../app/api");
const MUTATING_METHODS = ["POST", "PUT", "PATCH", "DELETE"] as const;

function findRouteFiles(dir: string): string[] {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  const files: string[] = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...findRouteFiles(full));
    } else if (entry.name === "route.ts") {
      files.push(full);
    }
  }
  return files;
}

function exportedMutatingMethods(source: string): string[] {
  return MUTATING_METHODS.filter((method) =>
    new RegExp(`export\\s+(async\\s+)?function\\s+${method}\\b|export\\s+const\\s+${method}\\b`).test(
      source
    )
  );
}

// Slices out one exported handler's own body, from its `export ... METHOD`
// declaration up to the next top-level `export` (or EOF). Route files can
// export several mutating methods in one module (e.g. PUT/DELETE/PATCH in a
// settings [id] route) — checking the guard is called *somewhere* in the
// file isn't enough to prove a specific handler calls it.
function handlerSlice(source: string, method: string): string {
  const declPattern = new RegExp(
    `export\\s+(async\\s+)?function\\s+${method}\\b|export\\s+const\\s+${method}\\b`
  );
  const declMatch = declPattern.exec(source);
  if (!declMatch) {
    throw new Error(`expected an export declaration for ${method} but found none`);
  }
  const bodyStart = declMatch.index + declMatch[0].length;
  const nextExport = /\nexport\s/.exec(source.slice(bodyStart));
  const bodyEnd = nextExport ? bodyStart + nextExport.index : source.length;
  return source.slice(declMatch.index, bodyEnd);
}

describe("write guard coverage — every mutating API route", () => {
  const routeFiles = findRouteFiles(API_ROOT);

  it("finds route.ts files to check (sanity check the walk itself works)", () => {
    expect(routeFiles.length).toBeGreaterThan(10);
  });

  const mutatingRoutes = routeFiles
    .map((file) => ({ file, source: fs.readFileSync(file, "utf8") }))
    .map(({ file, source }) => ({
      file,
      source,
      methods: exportedMutatingMethods(source),
    }))
    .filter((r) => r.methods.length > 0);

  it("finds at least one mutating route (sanity check the detector itself works)", () => {
    expect(mutatingRoutes.length).toBeGreaterThan(10);
  });

  // Sanity check the detector: at least one route file in this codebase
  // exports more than one mutating method, so the per-handler slicing below
  // is actually exercised against a multi-method file, not just single-POST
  // routes where "somewhere in the file" and "in this handler" coincide.
  it("finds at least one route file exporting multiple mutating methods", () => {
    expect(mutatingRoutes.some((r) => r.methods.length > 1)).toBe(true);
  });

  const IMPORTS_GUARD_DIRECTLY =
    /import\s*\{[^}]*\bisTrustedWriteContext\b[^}]*\}\s*from\s*["']@\/lib\/write-guard["']/;

  it.each(mutatingRoutes.map((r) => [path.relative(API_ROOT, r.file), r] as const))(
    "%s imports isTrustedWriteContext",
    (_label, route) => {
      expect(IMPORTS_GUARD_DIRECTLY.test(route.source)).toBe(true);
    }
  );

  const perHandlerCases = mutatingRoutes.flatMap((route) =>
    route.methods.map(
      (method) =>
        [`${path.relative(API_ROOT, route.file)} ${method}`, route, method] as const
    )
  );

  it.each(perHandlerCases)(
    "%s calls isTrustedWriteContext in its own handler body",
    (_label, route, method) => {
      const slice = handlerSlice(route.source, method);
      expect(/isTrustedWriteContext\s*\(/.test(slice)).toBe(true);
    }
  );
});
