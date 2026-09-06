import { describe, expect, it } from "vitest";
import { startReviewClock, transitionReviewClock } from "@/lib/review-clock";

describe("active-visible-idle60/v1", () => {
  it("counts active review only up to the idle cutoff", () => {
    const clock = transitionReviewClock(startReviewClock(100, true), "tick", 90_100);
    expect(clock.elapsedMs).toBe(60_000);
  });
  it("does not backfill the idle gap when activity resumes", () => {
    const idle = transitionReviewClock(startReviewClock(0, true), "activity", 90_000);
    expect(transitionReviewClock(idle, "tick", 92_000).elapsedMs).toBe(62_000);
  });
  it("pauses hidden tabs and resumes on visibility", () => {
    const hidden = transitionReviewClock(startReviewClock(0, true), "hide", 1000);
    const visible = transitionReviewClock(hidden, "show", 9000);
    expect(transitionReviewClock(visible, "tick", 10_000).elapsedMs).toBe(2000);
  });
  it("pauses manually and never counts submission or retries", () => {
    const paused = transitionReviewClock(startReviewClock(0, true), "pause", 1000);
    const resumed = transitionReviewClock(paused, "resume", 10_000);
    const submitted = transitionReviewClock(resumed, "submit", 11_000);
    expect(transitionReviewClock(submitted, "tick", 80_000).elapsedMs).toBe(2000);
  });
  it("a saved action resets timing, while a known failed save retains time", () => {
    const submitted = transitionReviewClock(startReviewClock(0, true), "submit", 1000);
    const failed = transitionReviewClock(submitted, "failed", 3000);
    expect(transitionReviewClock(failed, "tick", 4000).elapsedMs).toBe(2000);
    expect(transitionReviewClock(submitted, "saved", 3000).elapsedMs).toBe(0);
  });
  it("restores measured time without counting time away", () => {
    expect(transitionReviewClock(startReviewClock(100_000, true, 3000), "tick", 101_000).elapsedMs).toBe(4000);
  });
});
