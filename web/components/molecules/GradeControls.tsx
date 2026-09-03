"use client";

import { useState, useCallback, useEffect, useId, useRef } from "react";
import { Check, X } from "lucide-react";
import type {
  ActionJudgment,
  Grade,
  GradeInput,
  FailureDimension,
  Posture,
  RelevanceJudgment,
} from "@/types/schema";

const DIMENSION_OPTIONS: { label: string; value: FailureDimension }[] = [
  { label: "Context", value: "contextual_understanding" },
  { label: "Factual", value: "factual_support" },
  { label: "Implication", value: "unsupported_implication" },
  { label: "Posture", value: "posture" },
  { label: "Tone", value: "tone" },
  { label: "Wording", value: "wording" },
  { label: "Usefulness", value: "usefulness" },
];

const POSTURE_OPTIONS: { label: string; value: Posture }[] = [
  { label: "Answer", value: "answer" },
  { label: "Engage", value: "engage" },
  { label: "Ask", value: "ask" },
  { label: "Abstain", value: "abstain" },
];

/**
 * The four-cell relevance judgment matrix, keyed by what the evaluation
 * predicted and what the operator's verdict says about actual relevance.
 */
function deriveRelevanceJudgment(
  predictedRelevant: boolean,
  humanSaysRelevant: boolean
): RelevanceJudgment {
  if (predictedRelevant) {
    return humanSaysRelevant ? "correct" : "false_positive";
  }
  return humanSaysRelevant ? "false_negative" : "correct";
}

interface GradeControlsProps {
  postId: number;
  scanId: number;
  evaluationId?: number;
  predictedRelevant: boolean;
  existingGrade: Grade | null;
  /** Undefined means no draft row; null is an existing draft with empty text. */
  draftComment?: string | null;
  onGradeChange?: (grade: Grade) => void;
}

function selectEditedText(
  draftComment: string | null | undefined,
  correctedReply: string
): string | null {
  if (draftComment === undefined) return null;
  const normalized = correctedReply.trim();
  if (!normalized || normalized === (draftComment ?? "").trim()) return null;
  return normalized;
}

interface FailExplanationInput {
  failureNote: string;
  editedText: string | null;
  persistedEditedText: string | null | undefined;
  dimensions: FailureDimension[];
}

function selectHasFailExplanation({
  failureNote,
  editedText,
  persistedEditedText,
  dimensions,
}: FailExplanationInput): boolean {
  const causalDimensions: FailureDimension[] = [
    "contextual_understanding",
    "factual_support",
    "posture",
    "unsupported_implication",
  ];
  return (
    failureNote.trim().length > 0 ||
    editedText !== null ||
    Boolean(persistedEditedText?.trim()) ||
    dimensions.some((dimension) => causalDimensions.includes(dimension))
  );
}

export function GradeControls({
  postId,
  scanId,
  evaluationId,
  predictedRelevant,
  existingGrade,
  draftComment,
  onGradeChange,
}: GradeControlsProps) {
  const correctedReplyId = useId();
  const [relevanceJudgment, setRelevanceJudgment] = useState<RelevanceJudgment | null>(
    existingGrade?.relevance_judgment ?? null
  );
  const [actionJudgment, setActionJudgment] = useState<ActionJudgment | null>(
    existingGrade?.action_judgment ?? null
  );
  const [dimensions, setDimensions] = useState<FailureDimension[]>(
    existingGrade?.dimensions ?? []
  );
  const [failureNote, setFailureNote] = useState(existingGrade?.failure_note ?? "");
  const [contextMissingInput, setContextMissingInput] = useState(
    existingGrade?.context_missing_input ?? ""
  );
  const [postureShouldHaveBeen, setPostureShouldHaveBeen] = useState<Posture | null>(
    existingGrade?.posture_should_have_been ?? null
  );
  const [factualClaim, setFactualClaim] = useState(
    existingGrade?.factual_offending_claim ?? ""
  );
  const [factualDisposition, setFactualDisposition] = useState<
    "unsupported" | "contradicted" | null
  >(existingGrade?.factual_disposition ?? null);
  const [factualEvidence, setFactualEvidence] = useState(
    existingGrade?.factual_contradicting_evidence ?? ""
  );
  const [implicationClaim, setImplicationClaim] = useState(
    existingGrade?.implication_implied_claim ?? ""
  );
  const [implicationSupport, setImplicationSupport] = useState(
    existingGrade?.implication_missing_support ?? ""
  );
  const [correctedReply, setCorrectedReply] = useState(
    existingGrade?.edited_text ?? draftComment ?? ""
  );
  const [saving, setSaving] = useState(false);
  const [generatingDraft, setGeneratingDraft] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const persistedRef = useRef<Grade | null>(existingGrade);

  const applyGrade = useCallback((grade: Grade | null) => {
    setRelevanceJudgment(grade?.relevance_judgment ?? null);
    setActionJudgment(grade?.action_judgment ?? null);
    setDimensions(grade?.dimensions ?? []);
    setFailureNote(grade?.failure_note ?? "");
    setContextMissingInput(grade?.context_missing_input ?? "");
    setPostureShouldHaveBeen(grade?.posture_should_have_been ?? null);
    setFactualClaim(grade?.factual_offending_claim ?? "");
    setFactualDisposition(grade?.factual_disposition ?? null);
    setFactualEvidence(grade?.factual_contradicting_evidence ?? "");
    setImplicationClaim(grade?.implication_implied_claim ?? "");
    setImplicationSupport(grade?.implication_missing_support ?? "");
  }, []);

  useEffect(() => {
    persistedRef.current = existingGrade;
    applyGrade(existingGrade);
    setCorrectedReply(existingGrade?.edited_text ?? draftComment ?? "");
    setSaveError(null);
  }, [existingGrade, draftComment, applyGrade]);

  const save = useCallback(
    async (input: GradeInput) => {
      const snapshot = persistedRef.current;
      const promotesNegativeCase =
        !predictedRelevant && input.relevance_judgment === "false_negative";
      setSaving(true);
      setGeneratingDraft(promotesNegativeCase);
      setSaveError(null);
      try {
        const endpoint = evaluationId
          ? promotesNegativeCase
            ? `/api/grades/${evaluationId}/promote`
            : `/api/grades/${evaluationId}`
          : `/api/scans/${scanId}/posts/${postId}/grade`;
        const res = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(input),
        });
        if (res.ok) {
          const grade = (await res.json()) as Grade;
          persistedRef.current = grade;
          applyGrade(grade);
          onGradeChange?.(grade);
        } else {
          // Promotion persists the source false-negative before inference.
          // Keep the operator's entered details visible when generation
          // fails so the same case can be retried without re-entering them.
          if (!promotesNegativeCase) applyGrade(snapshot);
          const errBody = (await res.json().catch(() => ({}))) as {
            detail?: string;
            errors?: unknown;
          };
          const errors = Array.isArray(errBody.errors)
            ? errBody.errors.filter((error): error is string => typeof error === "string")
            : [];
          setSaveError(errors.join(" ") || errBody.detail || `Save failed (${res.status})`);
        }
      } catch {
        if (!promotesNegativeCase) applyGrade(snapshot);
        setSaveError(
          promotesNegativeCase
            ? "Draft generation could not be completed — retry this case"
            : "Grade could not be saved — check your connection"
        );
      } finally {
        setSaving(false);
        setGeneratingDraft(false);
      }
    },
    [postId, scanId, evaluationId, predictedRelevant, onGradeChange, applyGrade]
  );

  const hasDraftRow = draftComment !== undefined;

  const handleYes = () => {
    const judgment = deriveRelevanceJudgment(predictedRelevant, true);
    setRelevanceJudgment(judgment);
    if (!predictedRelevant) {
      // Scout skipped a relevant post: the relevance decision failed, so the
      // shared contract requires causal failure details before it can save.
      setActionJudgment("fail");
      return;
    }
    setActionJudgment(hasDraftRow ? null : "accept");
    if (!hasDraftRow) {
      save({ relevance_judgment: judgment, action_judgment: "accept" });
    }
  };

  const handleNo = () => {
    const judgment = deriveRelevanceJudgment(predictedRelevant, false);
    setRelevanceJudgment(judgment);
    if (!predictedRelevant) {
      // Scout correctly skipped an irrelevant post. There is no response to
      // assess and no failure to explain, so accept the outcome immediately.
      setActionJudgment("accept");
      save({ relevance_judgment: judgment, action_judgment: "accept" });
      return;
    }
    setActionJudgment("fail");
  };

  const handleAccept = () => {
    setActionJudgment("accept");
    save({
      relevance_judgment: relevanceJudgment ?? deriveRelevanceJudgment(predictedRelevant, true),
      action_judgment: "accept",
    });
  };

  const handleFlagIssue = () => {
    setActionJudgment("fail");
  };

  const toggleDimension = (dim: FailureDimension) => {
    setDimensions((prev) =>
      prev.includes(dim) ? prev.filter((d) => d !== dim) : [...prev, dim]
    );
  };

  const handleSaveFail = () => {
    setActionJudgment("fail");
    const rj = relevanceJudgment ?? deriveRelevanceJudgment(predictedRelevant, false);
    const input: GradeInput = {
      relevance_judgment: rj,
      action_judgment: "fail",
      dimensions: dimensions.length > 0 ? dimensions : null,
      failure_note: failureNote || null,
    };
    const editedText = selectEditedText(
      existingGrade?.edited_text ?? draftComment,
      correctedReply
    );
    if (editedText !== null) input.edited_text = editedText;
    if (dimensions.includes("contextual_understanding") && contextMissingInput) {
      input.context_missing_input = contextMissingInput;
    }
    if (dimensions.includes("posture") && postureShouldHaveBeen) {
      input.posture_should_have_been = postureShouldHaveBeen;
    }
    if (dimensions.includes("factual_support") && factualClaim) {
      input.factual_offending_claim = factualClaim;
      if (factualDisposition) input.factual_disposition = factualDisposition;
      if (factualDisposition === "contradicted" && factualEvidence) {
        input.factual_contradicting_evidence = factualEvidence;
      }
    }
    if (dimensions.includes("unsupported_implication")) {
      if (implicationClaim) input.implication_implied_claim = implicationClaim;
      if (implicationSupport) input.implication_missing_support = implicationSupport;
    }
    save(input);
  };

  const humanSaysRelevant = predictedRelevant
    ? relevanceJudgment === "correct"
    : relevanceJudgment === "false_negative";
  const humanSaysIrrelevant = predictedRelevant
    ? relevanceJudgment === "false_positive"
    : relevanceJudgment === "correct";
  const isAccepted = humanSaysRelevant && actionJudgment === "accept";
  const inFailMode = actionJudgment === "fail";
  const hasDim = (d: FailureDimension) => dimensions.includes(d);
  const editedText = selectEditedText(
    existingGrade?.edited_text ?? draftComment,
    correctedReply
  );
  const hasFailExplanation = selectHasFailExplanation({
    failureNote,
    editedText,
    persistedEditedText: existingGrade?.edited_text,
    dimensions,
  });
  const missingFailRequirements = [
    ...(dimensions.length === 0 ? ["select at least one issue"] : []),
    ...(!hasFailExplanation ? ["add a failure note, correct the reply, or add causal detail"] : []),
    ...(hasDim("contextual_understanding") && !contextMissingInput.trim()
      ? ["describe the missing context"]
      : []),
    ...(hasDim("posture") && postureShouldHaveBeen === null
      ? ["choose the intended posture"]
      : []),
    ...(hasDim("factual_support") && !factualClaim.trim()
      ? ["identify the offending claim"]
      : []),
    ...(hasDim("factual_support") && factualDisposition === null
      ? ["mark the claim unsupported or contradicted"]
      : []),
    ...(hasDim("factual_support") &&
    factualDisposition === "contradicted" &&
    !factualEvidence.trim()
      ? ["add the contradicting evidence"]
      : []),
    ...(hasDim("unsupported_implication") && !implicationClaim.trim()
      ? ["describe the implied claim"]
      : []),
    ...(hasDim("unsupported_implication") && !implicationSupport.trim()
      ? ["explain what support is missing"]
      : []),
  ];
  const canSaveFail = missingFailRequirements.length === 0;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-xs font-medium uppercase text-gray-600 dark:text-gray-500">
          Should this post have been surfaced?
        </span>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={handleYes}
            disabled={saving}
            className={`flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs font-medium transition-all disabled:cursor-not-allowed disabled:opacity-40 ${
              humanSaysRelevant
                ? "border-green-300 bg-green-100 text-green-700 dark:border-green-500/30 dark:bg-green-500/20 dark:text-green-400"
                : "border-gray-300 bg-white text-gray-600 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:border-gray-600"
            }`}
          >
            <Check className="h-3 w-3" /> Yes
          </button>
          <button
            type="button"
            onClick={handleNo}
            disabled={saving}
            className={`flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs font-medium transition-all disabled:cursor-not-allowed disabled:opacity-40 ${
              humanSaysIrrelevant
                ? "border-red-300 bg-red-100 text-red-700 dark:border-red-500/30 dark:bg-red-500/20 dark:text-red-400"
                : "border-gray-300 bg-white text-gray-600 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:border-gray-600"
            }`}
          >
            <X className="h-3 w-3" /> No
          </button>
        </div>
        {saving && (
          <span className="text-xs text-gray-600 dark:text-gray-500">
            {generatingDraft ? "generating response draft..." : "saving..."}
          </span>
        )}
      </div>

      {saveError && <p className="text-xs text-red-600 dark:text-red-400">{saveError}</p>}

      {humanSaysRelevant && hasDraftRow && (
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-xs font-medium uppercase text-gray-600 dark:text-gray-500">
            Is the drafted response good?
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={handleAccept}
              className={`rounded-md border px-2.5 py-1 text-xs font-medium transition-all ${
                isAccepted
                  ? "border-green-300 bg-green-100 text-green-700 dark:border-green-500/30 dark:bg-green-500/20 dark:text-green-400"
                  : "border-gray-300 bg-white text-gray-600 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:border-gray-600"
              }`}
            >
              Looks good
            </button>
            <button
              type="button"
              onClick={handleFlagIssue}
              className={`rounded-md border px-2.5 py-1 text-xs font-medium transition-all ${
                inFailMode
                  ? "border-orange-300 bg-orange-100 text-orange-700 dark:border-orange-500/30 dark:bg-orange-500/20 dark:text-orange-400"
                  : "border-gray-300 bg-white text-gray-600 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:border-gray-600"
              }`}
            >
              Has issue
            </button>
          </div>
          {actionJudgment === null && (
            <span className="text-xs text-amber-700 dark:text-amber-400">
              Choose response quality to save this grade.
            </span>
          )}
        </div>
      )}

      {inFailMode && (
        <div className="space-y-3 pl-4">
          <span className="text-xs text-gray-600 dark:text-gray-500">
            {humanSaysIrrelevant
              ? "Why shouldn’t this post be surfaced?"
              : "What’s wrong with the drafted response?"}
          </span>

          <div className="flex flex-wrap gap-1.5">
            {DIMENSION_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => toggleDimension(opt.value)}
                className={`rounded-md border px-2 py-0.5 text-xs font-medium transition-all ${
                  hasDim(opt.value)
                    ? "border-orange-300 bg-orange-100 text-orange-700 dark:border-orange-500/30 dark:bg-orange-500/20 dark:text-orange-400"
                    : "border-gray-300 bg-white text-gray-600 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:border-gray-600"
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>

          {humanSaysRelevant && hasDraftRow && (
            <div className="space-y-1">
              <label
                htmlFor={correctedReplyId}
                className="block text-xs font-medium text-gray-600 dark:text-gray-500"
              >
                Corrected response
              </label>
              <textarea
                id={correctedReplyId}
                value={correctedReply}
                onChange={(event) => setCorrectedReply(event.target.value)}
                rows={6}
                className="w-full resize-y rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-700 placeholder-gray-400 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:placeholder-gray-600"
              />
              <span className="block text-xs text-gray-500 dark:text-gray-600">
                Edit Scout’s response to show what it should have said.
              </span>
            </div>
          )}

          {hasDim("contextual_understanding") && (
            <input
              type="text"
              placeholder="What context was missing from the input?"
              value={contextMissingInput}
              onChange={(e) => setContextMissingInput(e.target.value)}
              className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 placeholder-gray-400 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:placeholder-gray-600"
            />
          )}

          {hasDim("posture") && (
            <div className="space-y-1">
              <span className="text-xs text-gray-600 dark:text-gray-500">Should have been</span>
              <div className="flex flex-wrap gap-1.5">
                {POSTURE_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => setPostureShouldHaveBeen(opt.value)}
                    className={`rounded-md border px-2 py-0.5 text-xs font-medium transition-all ${
                      postureShouldHaveBeen === opt.value
                        ? "border-blue-300 bg-blue-100 text-blue-700 dark:border-blue-500/30 dark:bg-blue-500/20 dark:text-blue-400"
                        : "border-gray-300 bg-white text-gray-600 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:border-gray-600"
                    }`}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {hasDim("factual_support") && (
            <div className="space-y-1.5">
              <input
                type="text"
                placeholder="Offending claim"
                value={factualClaim}
                onChange={(e) => setFactualClaim(e.target.value)}
                className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 placeholder-gray-400 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:placeholder-gray-600"
              />
              <div className="flex gap-2">
                {(["unsupported", "contradicted"] as const).map((disp) => (
                  <button
                    key={disp}
                    type="button"
                    onClick={() => setFactualDisposition(disp)}
                    className={`rounded-md border px-2 py-0.5 text-xs font-medium capitalize transition-all ${
                      factualDisposition === disp
                        ? "border-blue-300 bg-blue-100 text-blue-700 dark:border-blue-500/30 dark:bg-blue-500/20 dark:text-blue-400"
                        : "border-gray-300 bg-white text-gray-600 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:border-gray-600"
                    }`}
                  >
                    {disp}
                  </button>
                ))}
              </div>
              {factualDisposition === "contradicted" && (
                <input
                  type="text"
                  placeholder="Evidence from dossier that contradicts this"
                  value={factualEvidence}
                  onChange={(e) => setFactualEvidence(e.target.value)}
                  className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 placeholder-gray-400 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:placeholder-gray-600"
                />
              )}
            </div>
          )}

          {hasDim("unsupported_implication") && (
            <div className="space-y-1.5">
              <input
                type="text"
                placeholder="Implied claim"
                value={implicationClaim}
                onChange={(e) => setImplicationClaim(e.target.value)}
                className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 placeholder-gray-400 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:placeholder-gray-600"
              />
              <input
                type="text"
                placeholder="What support is missing?"
                value={implicationSupport}
                onChange={(e) => setImplicationSupport(e.target.value)}
                className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 placeholder-gray-400 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:placeholder-gray-600"
              />
            </div>
          )}

          <input
            type="text"
            placeholder="Failure note..."
            value={failureNote}
            onChange={(e) => setFailureNote(e.target.value)}
            className="w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-700 placeholder-gray-400 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:placeholder-gray-600"
          />

          {!canSaveFail && (
            <p id="grade-save-requirements" className="text-xs text-amber-700 dark:text-amber-400">
              To save: {missingFailRequirements.join("; ")}.
            </p>
          )}

          <button
            type="button"
            onClick={handleSaveFail}
            disabled={saving || !canSaveFail}
            aria-describedby={!canSaveFail ? "grade-save-requirements" : undefined}
            className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 transition-all hover:border-gray-400 disabled:opacity-40 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-gray-600"
          >
            {!predictedRelevant && humanSaysRelevant ? "Save & generate draft" : "Save"}
          </button>
        </div>
      )}
    </div>
  );
}
