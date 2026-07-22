import { boundedMessageWindow, MESSAGE_WINDOW_SIZE } from "./messageWindow";

describe("boundedMessageWindow", () => {
  it("keeps a 10,000-message conversation DOM bounded to one window", () => {
    const messages = Array.from({ length: 10_000 }, (_, index) => ({ id: index }));
    const started = performance.now();
    const page = boundedMessageWindow(messages);
    expect(page).toHaveLength(MESSAGE_WINDOW_SIZE);
    expect(page[0].id).toBe(9_800);
    expect(performance.now() - started).toBeLessThan(50);
  });
});
