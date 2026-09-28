// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { GradeControls } from "@/components/molecules/GradeControls";
import type { Grade } from "@/types/schema";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function makeGrade(overrides: Partial<Grade> = {}): Grade {
  return {
    id: 1,
    evaluation_id: 10,
    post_id: 1,
    scan_id: 2,
    source: "web",
    graded_at: "2026-01-01T00:00:00Z",
    schema_version: 2,
    needs_regrade: 0,
    relevance_judgment: "correct",
    action_judgment: "accept",
    dimensions: null,
    failure_note: null,
    factual_offending_claim: null,
    factual_disposition: null,
    factual_contradicting_evidence: null,
    context_missing_input: null,
    posture_should_have_been: null,
    implication_implied_claim: null,
    implication_missing_support: null,
    reply_revision_id: null,
    ...overrides,
  };
}

describe("GradeControls", () => {
  it("keeps fail save disabled until selected causal details are complete", () => {
    const { getByRole, getByPlaceholderText, getByText, queryByText } = render(
      React.createElement(GradeControls, {
        postId: 1,
        scanId: 2,
        predictedRelevant: true,
        existingGrade: null,
        draftComment: undefined,
      })
    );

    fireEvent.click(getByRole("button", { name: /no/i }));
    fireEvent.click(getByRole("button", { name: "Context" }));
    fireEvent.change(getByPlaceholderText("Failure note..."), {
      target: { value: "The response missed context" },
    });

    expect((getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    expect(getByText(/To save: describe the missing context/i)).toBeTruthy();

    fireEvent.change(
      getByPlaceholderText("What context was missing from the input?"),
      { target: { value: "The immediate parent post" } }
    );

    expect((getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(false);
    expect(queryByText(/^To save:/i)).toBeNull();
  });

  it("grades response quality explicitly after relevance", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => makeGrade(),
    } as Response);
    vi.stubGlobal("fetch", fetchMock);

    const { getByRole, getByText } = render(
      React.createElement(GradeControls, {
        postId: 1,
        scanId: 2,
        evaluationId: 10,
        predictedRelevant: true,
        existingGrade: null,
        draftComment: "Draft comment",
      })
    );

    fireEvent.click(getByRole("button", { name: /yes/i }));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(getByText(/Choose response quality to save this grade/i)).toBeTruthy();

    fireEvent.click(getByRole("button", { name: /looks good/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/grades/10",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            relevance_judgment: "correct",
            action_judgment: "accept",
          }),
        })
      );
    });
  });

  it("saves a response-quality issue after relevance is confirmed", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () =>
        makeGrade({
          action_judgment: "fail",
          dimensions: ["tone"],
          failure_note: "Too many em dashes",
        }),
    } as Response);
    vi.stubGlobal("fetch", fetchMock);

    const { getByRole, getByPlaceholderText } = render(
      React.createElement(GradeControls, {
        postId: 1,
        scanId: 2,
        evaluationId: 10,
        predictedRelevant: true,
        existingGrade: null,
        draftComment: "Draft comment",
      })
    );

    fireEvent.click(getByRole("button", { name: /yes/i }));
    fireEvent.click(getByRole("button", { name: /has issue/i }));
    fireEvent.click(getByRole("button", { name: "Tone" }));
    fireEvent.change(getByPlaceholderText("Failure note..."), {
      target: { value: "Too many em dashes" },
    });
    fireEvent.click(getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/grades/10",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            relevance_judgment: "correct",
            action_judgment: "fail",
            dimensions: ["tone"],
            failure_note: "Too many em dashes",
          }),
        })
      );
    });
  });

  it("offers wording and copy edits as a response issue", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => makeGrade({
        action_judgment: "fail",
        dimensions: ["wording"],
        failure_note: "Change a word and remove the comma",
      }),
    } as Response);
    vi.stubGlobal("fetch", fetchMock);

    const { getByRole, getByPlaceholderText } = render(
      React.createElement(GradeControls, {
        postId: 1,
        scanId: 2,
        evaluationId: 10,
        predictedRelevant: true,
        existingGrade: null,
        draftComment: "Draft comment",
      })
    );

    fireEvent.click(getByRole("button", { name: /yes/i }));
    fireEvent.click(getByRole("button", { name: /has issue/i }));
    fireEvent.click(getByRole("button", { name: "Wording" }));
    fireEvent.change(getByPlaceholderText("Failure note..."), {
      target: { value: "Change a word and remove the comma" },
    });
    fireEvent.click(getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/grades/10",
        expect.objectContaining({
          body: JSON.stringify({
            relevance_judgment: "correct",
            action_judgment: "fail",
            dimensions: ["wording"],
            failure_note: "Change a word and remove the comma",
          }),
        })
      );
    });
  });

  it("submits a changed draft as edited_text without requiring a failure note", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () =>
        makeGrade({
          action_judgment: "fail",
          dimensions: ["tone"],
          failure_note: null,
          edited_text: "A clearer corrected reply",
          reply_revision_id: 42,
        }),
    } as Response);
    vi.stubGlobal("fetch", fetchMock);

    const { getByRole, getByLabelText } = render(
      React.createElement(GradeControls, {
        postId: 1,
        scanId: 2,
        evaluationId: 10,
        predictedRelevant: true,
        existingGrade: null,
        draftComment: "Draft comment",
      })
    );

    fireEvent.click(getByRole("button", { name: /yes/i }));
    fireEvent.click(getByRole("button", { name: /has issue/i }));
    fireEvent.click(getByRole("button", { name: "Tone" }));
    fireEvent.change(getByLabelText("Corrected response"), {
      target: { value: "  A clearer corrected reply  " },
    });
    fireEvent.click(getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/grades/10",
        expect.objectContaining({
          body: JSON.stringify({
            relevance_judgment: "correct",
            action_judgment: "fail",
            dimensions: ["tone"],
            failure_note: null,
            edited_text: "A clearer corrected reply",
          }),
        })
      );
    });
  });

  it("allows a saved correction to be regraded without changing the reply again", () => {
    const { getByRole, getByLabelText } = render(
      React.createElement(GradeControls, {
        postId: 1,
        scanId: 2,
        evaluationId: 10,
        predictedRelevant: true,
        existingGrade: makeGrade({
          action_judgment: "fail",
          dimensions: ["tone"],
          failure_note: null,
          edited_text: "A previously corrected reply",
          reply_revision_id: 42,
        }),
        draftComment: "Draft comment",
      })
    );

    expect((getByLabelText("Corrected response") as HTMLTextAreaElement).value).toBe(
      "A previously corrected reply"
    );
    expect((getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("accepts complete causal detail without a failure note or reply edit", () => {
    const { getByRole, getByPlaceholderText } = render(
      React.createElement(GradeControls, {
        postId: 1,
        scanId: 2,
        evaluationId: 10,
        predictedRelevant: true,
        existingGrade: null,
        draftComment: "Draft comment",
      })
    );

    fireEvent.click(getByRole("button", { name: /yes/i }));
    fireEvent.click(getByRole("button", { name: /has issue/i }));
    fireEvent.click(getByRole("button", { name: "Context" }));
    fireEvent.change(getByPlaceholderText("What context was missing from the input?"), {
      target: { value: "The parent post" },
    });

    expect((getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("does not offer reply editing when the evaluation has no draft", () => {
    const { getByRole, queryByLabelText } = render(
      React.createElement(GradeControls, {
        postId: 1,
        scanId: 2,
        predictedRelevant: true,
        existingGrade: null,
        draftComment: undefined,
      })
    );

    fireEvent.click(getByRole("button", { name: /no/i }));

    expect(queryByLabelText("Corrected response")).toBeNull();
  });

  it("shows a save-error message near the control when the API rejects the grade", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => ({ detail: "database error" }),
      } as Response)
    );

    const { getByRole, findByText } = render(
      React.createElement(GradeControls, {
        postId: 1,
        scanId: 2,
        predictedRelevant: true,
        existingGrade: null,
        draftComment: undefined,
      })
    );

    fireEvent.click(getByRole("button", { name: /yes/i }));

    const errorMsg = await findByText(/database error/i);
    expect(errorMsg).toBeTruthy();
  });

  it("disables relevance choices while a grade is saving", async () => {
    let resolveSave!: (response: Response) => void;
    const saveResponse = new Promise<Response>((resolve) => {
      resolveSave = resolve;
    });
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(saveResponse));

    const { getByRole } = render(
      React.createElement(GradeControls, {
        postId: 1,
        scanId: 2,
        evaluationId: 10,
        predictedRelevant: false,
        existingGrade: null,
        draftComment: undefined,
      })
    );

    const yesButton = getByRole("button", { name: /yes/i }) as HTMLButtonElement;
    const noButton = getByRole("button", { name: /no/i }) as HTMLButtonElement;
    fireEvent.click(noButton);

    expect(yesButton.disabled).toBe(true);
    expect(noButton.disabled).toBe(true);

    resolveSave({
      ok: true,
      json: async () => makeGrade({ action_judgment: "accept" }),
    } as Response);

    await waitFor(() => {
      expect(yesButton.disabled).toBe(false);
      expect(noButton.disabled).toBe(false);
    });
  });

  it("rolls failed saves back to the latest existingGrade prop", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        json: async () => ({ errors: ["failed"] }),
      } as Response)
    );

    const { getByRole, getByText, queryByText, rerender } = render(
      React.createElement(GradeControls, {
        postId: 1,
        scanId: 2,
        predictedRelevant: true,
        existingGrade: makeGrade(),
        draftComment: "Draft comment",
      })
    );

    expect(queryByText("Why shouldn’t this post be surfaced?")).toBeNull();

    rerender(
      React.createElement(GradeControls, {
        postId: 1,
        scanId: 2,
        predictedRelevant: true,
        existingGrade: makeGrade({
          relevance_judgment: "false_positive",
          action_judgment: "fail",
          dimensions: ["contextual_understanding"],
          failure_note: "post was off-topic for this audience",
          context_missing_input: "channel is for announcements only",
        }),
        draftComment: "Draft comment",
      })
    );

    expect(getByText("Why shouldn’t this post be surfaced?")).toBeTruthy();
    expect(queryByText("Corrected response")).toBeNull();

    fireEvent.click(getByRole("button", { name: /yes/i }));
    fireEvent.click(getByRole("button", { name: /looks good/i }));

    await waitFor(() => {
      expect(getByText("Why shouldn’t this post be surfaced?")).toBeTruthy();
    });
  });

  describe("relevance judgment matrix", () => {
    it("derives correct when the evaluation predicted relevant and the operator says yes", async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeGrade(),
      } as Response);
      vi.stubGlobal("fetch", fetchMock);

      const { getByRole } = render(
        React.createElement(GradeControls, {
          postId: 1,
          scanId: 2,
          evaluationId: 10,
          predictedRelevant: true,
          existingGrade: null,
          draftComment: undefined,
        })
      );

      fireEvent.click(getByRole("button", { name: /yes/i }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          "/api/grades/10",
          expect.objectContaining({
            body: JSON.stringify({
              relevance_judgment: "correct",
              action_judgment: "accept",
            }),
          })
        );
      });
    });

    it("derives false_positive when the evaluation predicted relevant and the operator says no", async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        json: async () =>
          makeGrade({ relevance_judgment: "false_positive", action_judgment: "fail" }),
      } as Response);
      vi.stubGlobal("fetch", fetchMock);

      const { getByRole, getByPlaceholderText } = render(
        React.createElement(GradeControls, {
          postId: 1,
          scanId: 2,
          evaluationId: 10,
          predictedRelevant: true,
          existingGrade: null,
          draftComment: undefined,
        })
      );

      fireEvent.click(getByRole("button", { name: /no/i }));
      fireEvent.click(getByRole("button", { name: "Context" }));
      fireEvent.change(
        getByPlaceholderText("What context was missing from the input?"),
        { target: { value: "The immediate parent post" } }
      );
      fireEvent.change(getByPlaceholderText("Failure note..."), {
        target: { value: "Not actually about the topic" },
      });
      fireEvent.click(getByRole("button", { name: "Save" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          "/api/grades/10",
          expect.objectContaining({
            body: JSON.stringify({
              relevance_judgment: "false_positive",
              action_judgment: "fail",
              dimensions: ["contextual_understanding"],
              failure_note: "Not actually about the topic",
              context_missing_input: "The immediate parent post",
            }),
          })
        );
      });
    });

    it("derives correct when the evaluation predicted irrelevant and the operator says no", async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeGrade({ action_judgment: "accept" }),
      } as Response);
      vi.stubGlobal("fetch", fetchMock);

      const { getByRole } = render(
        React.createElement(GradeControls, {
          postId: 1,
          scanId: 2,
          evaluationId: 10,
          predictedRelevant: false,
          existingGrade: null,
          draftComment: undefined,
        })
      );

      fireEvent.click(getByRole("button", { name: /no/i }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          "/api/grades/10",
          expect.objectContaining({
            body: JSON.stringify({
              relevance_judgment: "correct",
              action_judgment: "accept",
            }),
          })
        );
      });
    });

    it("derives false_negative when the evaluation predicted irrelevant and the operator says yes", async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeGrade({
          relevance_judgment: "false_negative",
          action_judgment: "fail",
          dimensions: ["usefulness"],
          failure_note: "Scout should have surfaced this",
        }),
      } as Response);
      vi.stubGlobal("fetch", fetchMock);

      const { getByRole, getByPlaceholderText } = render(
        React.createElement(GradeControls, {
          postId: 1,
          scanId: 2,
          evaluationId: 10,
          predictedRelevant: false,
          existingGrade: null,
          draftComment: undefined,
        })
      );

      fireEvent.click(getByRole("button", { name: /yes/i }));
      expect(fetchMock).not.toHaveBeenCalled();
      fireEvent.click(getByRole("button", { name: "Usefulness" }));
      fireEvent.change(getByPlaceholderText("Failure note..."), {
        target: { value: "Scout should have surfaced this" },
      });
      fireEvent.click(getByRole("button", { name: "Save & generate draft" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          "/api/grades/10/promote",
          expect.objectContaining({
            body: JSON.stringify({
              relevance_judgment: "false_negative",
              action_judgment: "fail",
              dimensions: ["usefulness"],
              failure_note: "Scout should have surfaced this",
            }),
          })
        );
      });
    });
  });
});
