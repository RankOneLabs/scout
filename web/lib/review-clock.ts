// Monotonic browser timing. Hidden, paused, idle, and submitting time is excluded.
export const REVIEW_IDLE_MS = 60_000;
export interface ReviewClock {
  elapsedMs: number;
  checkpoint: number;
  lastActivity: number;
  visible: boolean;
  paused: boolean;
  submitting: boolean;
}
export type ReviewClockEvent = "tick" | "activity" | "hide" | "show" | "pause" | "resume" | "submit" | "saved" | "failed";

export function startReviewClock(now: number, visible: boolean, elapsedMs = 0): ReviewClock {
  return { elapsedMs, checkpoint: now, lastActivity: now, visible, paused: false, submitting: false };
}

export function transitionReviewClock(clock: ReviewClock, event: ReviewClockEvent, now: number): ReviewClock {
  const time = Math.max(now, clock.checkpoint);
  const activeEnd = Math.min(time, clock.lastActivity + REVIEW_IDLE_MS);
  const elapsedMs = clock.elapsedMs + (clock.visible && !clock.paused && !clock.submitting
    ? Math.max(0, activeEnd - clock.checkpoint) : 0);
  const next = { ...clock, elapsedMs, checkpoint: time };
  switch (event) {
    case "activity": return { ...next, lastActivity: time };
    case "hide": return { ...next, visible: false };
    case "show": return { ...next, visible: true, lastActivity: time };
    case "pause": return { ...next, paused: true };
    case "resume": return { ...next, paused: false, lastActivity: time };
    case "submit": return { ...next, submitting: true };
    case "saved": return { ...next, elapsedMs: 0, submitting: false, lastActivity: time };
    case "failed": return { ...next, submitting: false, lastActivity: time };
    default: return next;
  }
}
