import { describe, expect, it } from "vitest";

import { documentUploadedLabel, documentVersionLabel, generationResponseIsTerminal, hasRunningAssistantMessage, normalizeSourceCitations, panelStateFromMessageRecords, resultFromMessageRecord, sourceLocatorLabel, visibleNavigationChats } from "./App";


describe("persisted generation state", () => {
  it("shows upload time and conservative version status", () => {
    expect(documentUploadedLabel()).toBe("Время загрузки неизвестно");
    expect(documentVersionLabel({ version_label: null, version_status: "unknown" })).toBe("Версия не указана");
    expect(documentVersionLabel({
      version_label: "1.4",
      version_status: "superseded",
      newer_file_name: "Регламент v1.5.docx"
    })).toBe("Есть новее: Регламент v1.5.docx");
  });

  it("keeps long chat histories compact until the user expands them", () => {
    const chats = ["one", "two", "three", "four", "five", "six"];
    expect(visibleNavigationChats(chats, false)).toEqual(["one", "two", "three", "four"]);
    expect(visibleNavigationChats(chats, false, 4, 5)).toEqual(["one", "two", "three", "six"]);
    expect(visibleNavigationChats(chats, true)).toEqual(chats);
  });

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
      url: "/api/documents/doc-1/content"
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

  it("restores the technical panel and its matching question after refresh", () => {
    const restored = panelStateFromMessageRecords([
      {
        id: "question-1",
        chat_id: "chat-1",
        role: "user",
        content: "Что такое ИВ0?",
        status: "complete"
      },
      {
        id: "answer-1",
        chat_id: "chat-1",
        role: "assistant",
        content: "ИВ0 — величина внешней инициативы.",
        status: "complete",
        metadata: {
          sources: [{ file: "r12.docx", locator: "раздел «ИВ0-1»", url: "/api/documents/r12/content" }],
          formulas: [{ text: "ИВ0 = V_УДГ - V_план.ДГ" }],
          usage: { total_tokens_est: 120, context_tokens_est: 80, answer_tokens_est: 40 }
        }
      }
    ]);

    expect(restored.question).toBe("Что такое ИВ0?");
    expect(restored.result?.sources?.[0].file).toBe("r12.docx");
    expect(restored.result?.formulas?.[0].text).toContain("ИВ0");
    expect(restored.result?.usage?.context_tokens_est).toBe(80);
  });

  it("keeps the latest completed answer paired with its own question", () => {
    const restored = panelStateFromMessageRecords([
      { id: "q1", chat_id: "chat-1", role: "user", content: "Первый вопрос", status: "complete" },
      { id: "a1", chat_id: "chat-1", role: "assistant", content: "Первый ответ", status: "complete" },
      { id: "q2", chat_id: "chat-1", role: "user", content: "Второй вопрос", status: "complete" },
      { id: "a2", chat_id: "chat-1", role: "assistant", content: "Второй ответ", status: "complete" }
    ]);

    expect(restored.question).toBe("Второй вопрос");
    expect(restored.result?.answer).toBe("Второй ответ");
  });
});
