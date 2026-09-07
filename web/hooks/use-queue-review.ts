"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { z } from "zod";
import { startReviewClock, transitionReviewClock, type ReviewClock, type ReviewClockEvent } from "@/lib/review-clock";
import type { QueueReviewItem, ReviewAction, ReviewPriceBasis, ReviewRequest, ReviewResult } from "@/types/review-queues";

const sessionSchema = z.object({ elapsedMs: z.number().nonnegative(), pendingBody: z.string().nullable() });
const errorResponseSchema = z.object({ detail: z.string().optional() });
const PERSIST_ERROR = "Cannot persist review timing. Reload with session storage available before reviewing.";

function createReviewActionId(): ReviewResult<ReviewRequest["action_id"]> {
  try {
    if (typeof crypto.randomUUID === "function") return { ok: true, value: crypto.randomUUID() };
    // randomUUID requires a secure context; getRandomValues also works on HTTP.
    // Preserve the UUID v4 wire contract with cryptographically secure bytes.
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return { ok: true, value: `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}` };
  } catch {
    return { ok: false, error: "Cannot generate a secure review action ID. Reload in a browser with Web Crypto available." };
  }
}

export function useQueueReview(input: {
  digest: string; item: QueueReviewItem; pricing: ReviewPriceBasis | null; onSaved: () => Promise<void>;
}) {
  const key = `scout-review/v1/${input.digest}/${input.item.source.evaluation_id}`;
  const clock = useRef<ReviewClock | null>(null);
  const pendingBody = useRef<string | null>(null);
  const busyRef = useRef(false);
  const storageError = useRef<string | null>(null);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [hasPending, setHasPending] = useState(false);
  const [paused, setPaused] = useState(false);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const transition = useCallback((event: ReviewClockEvent) => {
    if (!clock.current || storageError.current !== null) return false;
    clock.current = transitionReviewClock(clock.current, event, performance.now());
    setElapsedMs(Math.floor(clock.current.elapsedMs));
    setPaused(clock.current.paused);
    // A pending request retains its original timing bytes across retry/reload.
    try {
      sessionStorage.setItem(key, JSON.stringify({ elapsedMs: clock.current.elapsedMs, pendingBody: pendingBody.current }));
      return true;
    } catch {
      storageError.current = PERSIST_ERROR;
      setReady(false);
      setError(PERSIST_ERROR);
      return false;
    }
  }, [key]);

  useEffect(() => {
    setReady(false);
    storageError.current = null;
    let restored: z.infer<typeof sessionSchema> = { elapsedMs: 0, pendingBody: null };
    try {
      const raw = sessionStorage.getItem(key);
      if (raw) restored = sessionSchema.parse(JSON.parse(raw));
    } catch {
      // Corrupt or unavailable session storage must not invent measured time.
      setError("Cannot restore review timing. Reload with session storage available before reviewing.");
      return;
    }
    pendingBody.current = restored.pendingBody;
    clock.current = startReviewClock(performance.now(), !document.hidden, restored.elapsedMs);
    if (restored.pendingBody) clock.current = transitionReviewClock(clock.current, "submit", performance.now());
    setHasPending(restored.pendingBody !== null);
    setReady(true);
    const activity = () => transition("activity");
    const visibility = () => transition(document.hidden ? "hide" : "show");
    const leave = () => transition("hide");
    const interval = window.setInterval(() => transition("tick"), 1000);
    window.addEventListener("keydown", activity);
    window.addEventListener("pointerdown", activity);
    window.addEventListener("scroll", activity, true);
    window.addEventListener("pagehide", leave);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      transition("hide");
      clock.current = null;
      window.clearInterval(interval);
      window.removeEventListener("keydown", activity);
      window.removeEventListener("pointerdown", activity);
      window.removeEventListener("scroll", activity, true);
      window.removeEventListener("pagehide", leave);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [key, transition]);

  const send = useCallback(async () => {
    if (storageError.current !== null) throw new Error(storageError.current);
    if (busyRef.current || pendingBody.current === null) throw new Error("No retryable request, or save in progress");
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`/api/grading/queues/${input.digest}/evaluations/${input.item.source.evaluation_id}/actions`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: pendingBody.current,
      });
      if (!response.ok) {
        // A known rejection made no write. Transport/5xx failures remain pending.
        if (response.status >= 400 && response.status < 500) {
          pendingBody.current = null;
          setHasPending(false);
          transition("failed");
        }
        const body = errorResponseSchema.safeParse(await response.json().catch(() => null));
        throw new Error((body.success ? body.data.detail : null) ?? `Review save failed (${response.status})`);
      }
      // Refresh can fail after a committed save; retry remains the same action.
      await input.onSaved();
      pendingBody.current = null;
      setHasPending(false);
      if (!transition("saved")) throw new Error(PERSIST_ERROR);
    } catch (reason) {
      const message = storageError.current ?? (reason instanceof Error ? reason.message : "Save failed; retry the same action");
      setError(message);
      throw new Error(message);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, [input, transition]);

  const submit = useCallback(async (action: ReviewAction) => {
    if (storageError.current !== null) throw new Error(storageError.current);
    if (!ready || !clock.current || busyRef.current || pendingBody.current !== null) throw new Error("Resolve the pending request before another action");
    const actionId = createReviewActionId();
    if (!actionId.ok) {
      setError(actionId.error);
      throw new Error(actionId.error);
    }
    if (!transition("submit")) throw new Error(PERSIST_ERROR);
    const request: ReviewRequest = {
      action_id: actionId.value, expected_grade_revision_id: input.item.current_revision_id,
      expected_action_id: input.item.disposition?.action_id ?? null, action,
      timing: action.kind === "reconcile" ? { elapsed_ms: null, method: null } : {
        elapsed_ms: Math.floor(clock.current.elapsedMs), method: "active-visible-idle60/v1",
      },
      pricing: action.kind === "reconcile" ? null : input.pricing,
    };
    pendingBody.current = JSON.stringify(request);
    setHasPending(true);
    if (!transition("tick")) throw new Error(PERSIST_ERROR);
    await send();
  }, [input, ready, send, transition]);

  return { ready, busy, hasPending, paused, elapsedMs, error, submit, retry: send,
    togglePause: () => transition(paused ? "resume" : "pause") };
}
