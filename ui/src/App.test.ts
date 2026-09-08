import { describe, expect, it } from "vitest";

import { generationResponseIsTerminal, hasRunningAssistantMessage, resultFromMessageRecord } from "./App";


describe("persisted generation state", () => {
  it("detects an assistant generation restored after page refresh", () => {
    expect(hasRunningAssistantMessage([
      { role: "user", status: "complete" },
      { role: "assistant", status: "running" }
    ])).toBe(true);
  });

  it("stops polling after every assistant message is terminal", () => {
    expect(hasRunningAssistantMessage([
      { role: "assistant", status: "complete" },
      { role: "assistant", status: "error" }
    ])).toBe(false);
  });

  it("keeps chat generation active after a durable 202 response", () => {
    expect(generationResponseIsTerminal("running")).toBe(false);
    expect(generationResponseIsTerminal("complete")).toBe(true);
    expect(generationResponseIsTerminal()).toBe(true);
  });

  it("restores sources and formulas from persisted assistant metadata", () => {
    const restored = resultFromMessageRecord({
      id: "message-1",
      chat_id: "chat-1",
      role: "assistant",
      content: "Ответ",
      status: "complete",
      metadata: {
        sources: [{ file: "manual.pdf", url: "/api/documents/1/content", score: 0.9 }],
        formulas: [{ text: "Q = m c ΔT" }],
        from_cache: true
      }
    });
    expect(restored?.sources?.[0].url).toBe("/api/documents/1/content");
    expect(restored?.formulas?.[0].text).toContain("Q");
    expect(restored?.from_cache).toBe(true);
  });
});
