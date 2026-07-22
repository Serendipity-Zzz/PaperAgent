import { describe, expect, it } from "vitest";

import {
  DEFAULT_PREVIEW_VIEW_STATE,
  previewTabKey,
  upsertPreviewTab,
  type PreviewTabState,
} from "./tabStore";

function tab(id: string) {
  return {
    id,
    title: id,
    artifact: { id } as never,
    parts: [],
    viewState: { ...DEFAULT_PREVIEW_VIEW_STATE },
  };
}

describe("preview tab store", () => {
  it("uses project, artifact and revision as a stable identity", () => {
    expect(previewTabKey("p", "a", "r2")).toBe("p:a:r2");
    expect(previewTabKey("p", "a")).toBe("p:a:latest");
  });

  it("deduplicates functional updates and preserves view state", () => {
    const first = tab("p:a:r1");
    first.viewState.page = 4;
    const result = upsertPreviewTab([first], tab("p:a:r1"));
    expect(result).toHaveLength(1);
    expect(result[0].viewState.page).toBe(4);
  });

  it("keeps fifty distinct revisions without duplicate React keys", () => {
    let result: PreviewTabState[] = [];
    for (let index = 0; index < 50; index += 1) {
      result = upsertPreviewTab(result, tab(previewTabKey("p", "a", `r${index}`)));
    }
    expect(result).toHaveLength(50);
    expect(new Set(result.map((item) => item.id)).size).toBe(50);
  });
});
