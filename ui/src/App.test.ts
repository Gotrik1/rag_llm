import { describe, expect, it } from "vitest";

import { generationResponseIsTerminal, hasRunningAssistantMessage, normalizeSourceCitations, resultFromMessageRecord, sourceLocatorLabel } from "./App";


describe("persisted generation state", () => {
  it("shows a human document locator instead of retrieval metadata", () => {
    expect(sourceLocatorLabel({ locator: "стр. 17, раздел «Расчёт»" })).toBe("стр. 17, раздел «Расчёт»");
    expect(sourceLocatorLabel({ section: "Запуск" })).toBe("Раздел «Запуск»");
    expect(sourceLocatorLabel({})).toBe("Документ целиком");
  });

  it("upgrades persisted UUID citations to a linked document and locator", () => {
    const oldAnswer = "Факт. [Источник: 4f424eaf3d7149b2a8d7d668e1e49836_manual.pdf — Запуск]";
    expect(normalizeSourceCitations(oldAnswer, [{
      file: "manual.pdf",
      section: "Запуск",
      locator: "раздел «Запуск»",
      url: "/api/documents/doc-1/content",
      score: 0.9
    }])).toBe("Факт. [Источник: manual.pdf — раздел «Запуск»](/api/documents/doc-1/content)");
  });

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
