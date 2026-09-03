import type { DraftWithGrade, Grade } from "@/types/schema";

export function overlayGradesByEvaluation(
  drafts: DraftWithGrade[],
  gradeOverlay: Map<number, Grade>
): DraftWithGrade[] {
  if (gradeOverlay.size === 0) return drafts;
  return drafts.map((draft) => {
    if (draft.evaluation_id === undefined) return draft;
    const localGrade = gradeOverlay.get(draft.evaluation_id);
    return localGrade === undefined ? draft : { ...draft, grade: localGrade };
  });
}
