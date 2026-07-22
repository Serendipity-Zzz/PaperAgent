import { describe, expect, it } from "vitest";

import {
  deliveryProgress,
  mergePublicRunEvents,
  publicEventSummary,
  publicFailureMessage,
  runPhaseLabel,
} from "./runTraceModel";

describe("document delivery public progress", () => {
  it("merges replayed and out-of-order events idempotently", () => {
    const first = { id: 2, type: "delivery.rendering", data: { format: "pdf" } };
    let events = mergePublicRunEvents([], first);
    events = mergePublicRunEvents(events, { id: 1, type: "delivery.binding", data: { revision: 3 } });
    events = mergePublicRunEvents(events, first);
    expect(events.map((event) => event.id)).toEqual([1, 2]);
    expect(deliveryProgress(events)).toMatchObject({ targetFormat: "PDF", sourceRevision: "r3" });
  });

  it("uses public delivery labels and redacts secrets and local paths", () => {
    expect(runPhaseLabel({ id: "r", status: "running", current_phase: "validating" }))
      .toBe("正在执行交付校验");
    const summary = publicEventSummary({
      id: 1,
      type: "x",
      data: { summary: "api_key=sk-fake-secret-token-1234 at E:\\private\\report.pdf" },
    });
    expect(summary).not.toContain("sk-secret");
    expect(summary).not.toContain("E:\\private");
  });

  it("explains missing images without exposing a raw tool exception", () => {
    const message = publicFailureMessage("missing_asset", {
      expected_images: 5,
      bound_images: 0,
    });
    expect(message).toContain("要求 5 张图");
    expect(message).toContain("不会重新运行已成功的实验");
    expect(message).not.toContain("tool:");
  });

  it("labels presentation-only phases and offers classified recovery", () => {
    expect(runPhaseLabel({ id: "r", status: "running", current_phase: "presentation_resolution" }))
      .toBe("正在解析封面与页眉页脚要求");
    expect(runPhaseLabel({ id: "r", status: "running", current_phase: "quality_assurance" }))
      .toBe("正在校验封面、页眉页脚与交付文件");
    expect(publicFailureMessage("presentation_field_missing"))
      .toContain("只重渲染受影响格式");
  });
});
