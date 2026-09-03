import Ajv2020 from "ajv/dist/2020";
import type { ErrorObject } from "ajv";

import schema from "../grading_schema.json";

// Single Ajv instance compiled once at module load, in strict + allErrors
// mode, against the same web/grading_schema.json artifact that Python
// (grading.py), the sidecar, and the eval corpus consume. This is the sole
// TypeScript-side grading validator — no handwritten conditional rules
// live here; every verdict comes from the shared schema.
const ajv = new Ajv2020({ allErrors: true, strict: true });
const validateEnvelope = ajv.compile(schema);

function formatError(err: ErrorObject): string {
  const path = err.instancePath || "<root>";
  return `${path}: ${err.message ?? "is invalid"}`;
}

/**
 * Validate a raw grade request body against the shared validationEnvelope
 * schema, given the evaluation's actual stored posture (or null when
 * unknown). The payload is passed through unfiltered — additionalProperties
 * is false on the schema's gradePayload, so unknown/legacy fields (e.g. a
 * stale v1 `comment_quality`) are rejected by the contract itself rather
 * than silently dropped by a hand-picked allowlist.
 *
 * Returns a list of error strings (empty = valid).
 */
export function validateGradeEnvelope(
  payload: unknown,
  evaluationPosture: string | null = null
): string[] {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    return ["request body must be a JSON object"];
  }

  const envelope = {
    grade: payload,
    evaluation_posture: evaluationPosture,
  };

  const valid = validateEnvelope(envelope);
  if (valid) return [];
  return (validateEnvelope.errors ?? []).map(formatError);
}
