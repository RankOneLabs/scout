// @vitest-environment jsdom
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ReplyComparisonViewer } from "@/components/molecules/ReplyComparisonViewer";

describe("ReplyComparisonViewer", () => {
  it("renders HTML-like and multiline evidence as text with authoritative values", () => {
    const { container } = render(<ReplyComparisonViewer evidence={{ available: true, correction: "fix\n<script>alert(1)</script>", baseline_output: { reply: "old" }, candidate_output: { reply: "new" }, baseline_distance: 8, candidate_distance: 2, delta: -6 }} />);
    expect(screen.getByText(/Candidate minus baseline distance: -6/)).toBeTruthy();
    expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeTruthy();
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByRole("img", { name: "Candidate correction distance: 2" })).toBeTruthy();
  });

  it("states why evidence is unavailable", () => {
    render(<ReplyComparisonViewer evidence={{ available: false, reason: "output_incomplete" }} />);
    expect(screen.getByText(/Unavailable: output incomplete/)).toBeTruthy();
  });
});
