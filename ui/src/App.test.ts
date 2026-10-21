import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App, chatFromRecord, documentUploadedLabel, documentVersionLabel, generationResponseIsTerminal, hasRunningAssistantMessage, normalizeMarkdown, normalizeSourceCitations, panelStateFromMessageRecords, resultFromMessageRecord, sourceLocatorLabel, splitDocumentsByScope, uploadFeedback, uploadScopeFields, visibleNavigationChats } from "./App";

// These tests exercise workspace navigation; the editor viewport runs in the browser checks.
vi.mock("@assistant-ui/react", async () => {
  const { createElement } = await import("react");
  return {
    AssistantRuntimeProvider: ({ children }: { children?: import("react").ReactNode }) => createElement("div", null, children),
    useExternalStoreRuntime: () => ({}),
    ThreadPrimitive: { Root: "div", Viewport: "div", Empty: "div", Messages: () => null, ViewportFooter: "div" },
    ComposerPrimitive: {
      Root: "div",
      Input: ({ submitMode: _submitMode, ...props }: { submitMode?: string } & import("react").ComponentProps<"textarea">) => createElement("textarea", props),
      Send: "button"
    },
    MessagePrimitive: { Root: "div", Content: () => null },
    useMessage: vi.fn()
  };
});


describe("persisted generation state", () => {
  it("renders a persisted legacy LaTeX equation instead of exposing commands", () => {
    const markdown = String.raw`- \mathrm{ИВ0} = V_{\mathrm{УДГ}} - V_{\mathrm{план.ДГ}}`;

    expect(normalizeMarkdown(markdown)).toBe(String.raw`- $\text{ИВ0} = V_{\text{УДГ}} - V_{\text{план.ДГ}}$`);
  });

  it("renders persisted DOCX function notation instead of inline code", () => {
    const markdown = "Расчет: `S_sub(э1цк,g,i,n,m) = W(ээ,i,n,m) × T(пер1ст,i,n,m)`";

    expect(normalizeMarkdown(markdown)).toBe(
      String.raw`Расчет: $S_{\text{э1цк},g,i,n,m} = W_{\text{ээ},i,n,m} \times T_{\text{пер1ст},i,n,m}$`
    );
  });

  it("does not rewrite raw LaTeX examples inside fenced code", () => {
    const markdown = ["```", String.raw`\mathrm{ИВ0} = V_{\mathrm{УДГ}}`, "```"].join("\n");

    expect(normalizeMarkdown(markdown)).toBe(markdown);
  });

  it("removes nested dollar delimiters copied around display formulas", () => {
    const markdown = String.raw`$$$$S_{\text{э1цк}} = V_{\text{ЭЭ}} \times \text{Ц}_{\text{ПУНЦ}}$$$$`;

    expect(normalizeMarkdown(markdown)).toBe(
      String.raw`$$S_{\text{э1цк}} = V_{\text{ЭЭ}} \times \text{Ц}_{\text{ПУНЦ}}$$`
    );
  });

  it("does not add inline delimiters inside a valid display math block", () => {
    const markdown = ["$$", String.raw`\text{Ц}_{\text{ПУНЦ}} = \text{Ц}_{\text{ЭЭ}}`, "$$"].join("\n");

    expect(normalizeMarkdown(markdown)).toBe(markdown);
  });

  it("unwraps an extra formula delimiter pair inside a display math block", () => {
    const markdown = ["$$", String.raw`$$S_{\text{э6цк}} = V_{\text{ЭЭ}} \times \text{Ц}_{\text{ПУНЦ}}$$`, "$$"].join("\n");

    expect(normalizeMarkdown(markdown)).toBe(
      ["$$", String.raw`S_{\text{э6цк}} = V_{\text{ЭЭ}} \times \text{Ц}_{\text{ПУНЦ}}`, "$$"].join("\n")
    );
  });

  it("leaves nested delimiter examples inside fenced code unchanged", () => {
    const markdown = ["```latex", String.raw`$$$$S = V \times C$$$$`, "```"].join("\n");

    expect(normalizeMarkdown(markdown)).toBe(markdown);
  });

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

  it("replaces the queued upload notice with the authoritative ready state", () => {
    const result = {
      chunks: 0,
      queued: true,
      files: [{
        id: "document-1",
        file: "manual.docx",
        job_id: "job-1",
        status: "queued",
        url: "/api/documents/document-1/content",
        size_bytes: 1200
      }]
    };

    expect(uploadFeedback(result, [{
      id: "document-1",
      file_name: "manual.docx",
      status: "ready",
      scope: "project"
    }])).toEqual({
      state: "success",
      message: "1 файл готов и добавлен в базу знаний"
    });
  });

  it("shows truthful progress and terminal upload failures", () => {
    const result = {
      chunks: 0,
      queued: true,
      files: [
        { id: "document-1", file: "one.docx", job_id: "job-1", status: "queued", url: "/one", size_bytes: 1 },
        { id: "document-2", file: "two.docx", job_id: "job-2", status: "queued", url: "/two", size_bytes: 1 }
      ]
    };

    expect(uploadFeedback(result, [
      { id: "document-1", file_name: "one.docx", status: "ready", scope: "project" },
      { id: "document-2", file_name: "two.docx", status: "processing", scope: "project" }
    ])).toEqual({ state: "pending", message: "Готово 1 из 2 · остальные обрабатываются" });

    expect(uploadFeedback(result, [
      { id: "document-1", file_name: "one.docx", status: "ready", scope: "project" },
      { id: "document-2", file_name: "two.docx", status: "failed", scope: "project" }
    ])).toEqual({
      state: "error",
      message: "Не обработано: 1 из 2. Готово: 1. Проверьте статус документа слева."
    });
  });

  it("keeps shared and project documents in separate visible groups", () => {
    const grouped = splitDocumentsByScope([
      { id: "shared", file_name: "Общий регламент.pdf", status: "ready", scope: "global" },
      { id: "private", file_name: "Проектный расчёт.docx", status: "ready", scope: "project", project_id: "project-1" },
      { id: "chat", file_name: "Схема.png", status: "ready", scope: "project", chat_id: "chat-1" }
    ]);

    expect(grouped.global.map((document) => document.id)).toEqual(["shared"]);
    expect(grouped.project.map((document) => document.id)).toEqual(["private"]);
    expect(grouped.chat.map((document) => document.id)).toEqual(["chat"]);
  });

  it("maps each upload entry point to exactly one knowledge scope", () => {
    expect(uploadScopeFields("global", "project-1", "chat-1")).toEqual({});
    expect(uploadScopeFields("project", "project-1", "chat-1")).toEqual({ project_id: "project-1" });
    expect(uploadScopeFields("chat", "project-1", "chat-1")).toEqual({ chat_id: "chat-1" });
    expect(() => uploadScopeFields("project", "", "chat-1")).toThrow("Сначала откройте проект");
    expect(() => uploadScopeFields("chat", "project-1", "")).toThrow("Сначала откройте чат");
  });

  it("restores the per-chat retrieval focus and falls back safely", () => {
    expect(chatFromRecord({
      id: "chat-rre",
      project_id: "project-1",
      title: "Розница",
      metadata: { retrieval_focus: "rre" }
    }).retrievalFocus).toBe("rre");
    expect(chatFromRecord({
      id: "chat-auto",
      project_id: null,
      title: "Общий",
      metadata: { retrieval_focus: "invalid" }
    }).retrievalFocus).toBe("auto");
  });
});

describe("per-tab chat restoration", () => {
  const chatA = { id: "chat-a", project_id: "project-a", title: "Chat A" };
  const chatB = { id: "chat-b", project_id: "project-b", title: "Chat B" };
  const freeChat = { id: "chat-free", project_id: null, title: "Free chat" };
  let availableChats: { id: string; project_id: string | null; title: string }[];
  let serverWorkspace: { active_chat_id: string | null; active_project_id: string | null };
  let workspaceResponse: Promise<unknown> | undefined;
  let root: Root | undefined;
  let container: HTMLDivElement;
  const fetchMock = vi.fn();

  function jsonResponse(data: unknown) {
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      headers: new Headers({ "content-type": "application/json" }),
      json: async () => data
    };
  }

  async function mountAt(url: string) {
    window.history.replaceState({ previous: "kept" }, "", url);
    root = createRoot(container);
    await act(async () => root!.render(createElement(App)));
  }

  async function unmount() {
    await act(async () => root?.unmount());
    root = undefined;
  }

  async function click(element: Element | null) {
    expect(element).not.toBeNull();
    await act(async () => (element as HTMLElement).click());
  }

  async function openMenu(element: Element | null) {
    expect(element).not.toBeNull();
    await act(async () => (element as HTMLElement).focus());
  }

  function messageRequests() {
    return fetchMock.mock.calls.map(([path]) => path as string).filter((path) => path.endsWith("/messages"));
  }

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    availableChats = [chatA, chatB, freeChat];
    serverWorkspace = { active_chat_id: chatB.id, active_project_id: chatB.project_id };
    workspaceResponse = undefined;
    container = document.createElement("div");
    document.body.appendChild(container);
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === "/api/auth/session") return jsonResponse({
        authenticated: true,
        csrf_token: "csrf",
        user: { id: "user-1", email: "user@example.test", display_name: "User", is_admin: false, can_manage_global_documents: false }
      });
      if (path === "/api/workspace") return jsonResponse(workspaceResponse ? await workspaceResponse : serverWorkspace);
      if (path === "/api/projects") return jsonResponse({ items: [
        { id: "project-a", name: "Project A" },
        { id: "project-b", name: "Project B" }
      ] });
      if (path === "/api/chats" && options?.method === "POST") {
        const chat = { id: "new-free", project_id: null, title: "Новый чат" };
        availableChats = [...availableChats, chat];
        return jsonResponse(chat);
      }
      if (path === "/api/projects/project-a/chats" && options?.method === "POST") {
        const chat = { id: "new-project-chat", project_id: "project-a", title: "Новый чат" };
        availableChats = [...availableChats, chat];
        return jsonResponse(chat);
      }
      if (path === "/api/chats") return jsonResponse({ items: availableChats });
      if (options?.method === "DELETE") {
        availableChats = availableChats.filter((chat) => path !== `/api/chats/${chat.id}`);
        return jsonResponse({ deleted: true });
      }
      if (path === "/api/settings") {
        serverWorkspace = JSON.parse(options!.body as string);
        return jsonResponse(serverWorkspace);
      }
      if (path === "/api/models") return jsonResponse({ models: ["test-model"], selected: "test-model" });
      if (path === "/api/providers") return jsonResponse({ config: { provider: "ollama", model: "test-model" }, defaults: {} });
      if (path === "/api/system-prompts") return jsonResponse({ prompts: [], selected: "rag-grounded" });
      if (path === "/api/flow-mode") return jsonResponse({ mode: "python" });
      return jsonResponse({ items: [] });
    });
  });

  afterEach(async () => {
    await unmount();
    container.remove();
    fetchMock.mockReset();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  it("restores two independent tab URLs after the account default changes", async () => {
    await mountAt("/?chat=chat-a");
    expect(messageRequests()).toEqual(["/api/chats/chat-a/messages"]);
    const tabAUrl = window.location.href;
    await unmount();

    await mountAt("/?chat=chat-b");
    const tabBUrl = window.location.href;
    expect(messageRequests().slice(-1)[0]).toBe("/api/chats/chat-b/messages");
    await unmount();

    fetchMock.mockClear();
    await mountAt(tabAUrl);
    expect(serverWorkspace.active_chat_id).toBe("chat-b");
    expect(messageRequests()).toEqual(["/api/chats/chat-a/messages"]);
    expect(window.location.href).toBe(tabAUrl);
    expect(tabBUrl).toContain("chat=chat-b");
    expect(container.querySelector(".nav-item.active .nav-item-copy > span")?.textContent).toBe("Project A");
  });

  it("keeps the deep link intact while workspace bootstrap is pending", async () => {
    let resolveWorkspace!: (value: unknown) => void;
    workspaceResponse = new Promise((resolve) => { resolveWorkspace = resolve; });
    await mountAt("/?chat=chat-a#answer");
    expect(window.location.search).toBe("?chat=chat-a");
    expect(messageRequests()).toEqual([]);

    await act(async () => resolveWorkspace(serverWorkspace));
    expect(messageRequests()).toEqual(["/api/chats/chat-a/messages"]);
    expect(window.location.hash).toBe("#answer");
  });

  it.each(["deleted-chat", "foreign-chat"])("falls back safely from the unavailable URL chat %s", async (chatId) => {
    await mountAt(`/?chat=${chatId}`);
    expect(messageRequests()).toEqual(["/api/chats/chat-b/messages"]);
    expect(window.location.search).toBe("?chat=chat-b");
  });

  it("validates a stale shared default before fetching any chat messages", async () => {
    serverWorkspace.active_chat_id = "foreign-chat";
    await mountAt("/?chat=deleted-chat");
    expect(messageRequests()).toEqual(["/api/chats/chat-free/messages"]);
    expect(window.location.search).toBe("?chat=chat-free");
    expect(container.querySelector('[aria-label="Документы проекта"]')).toBeNull();
  });

  it("derives a project's document scope from the URL chat", async () => {
    await mountAt("/?chat=chat-a");
    const paths = fetchMock.mock.calls.map(([path]) => path);
    expect(paths).toContain("/api/projects/project-a/documents");
    expect(paths).not.toContain("/api/projects/project-b/documents");
  });

  it("keeps a free chat outside the shared default project", async () => {
    await mountAt("/?chat=chat-free");
    expect(messageRequests()).toEqual(["/api/chats/chat-free/messages"]);
    expect(container.querySelector('[aria-label="Документы проекта"]')).toBeNull();
    expect(fetchMock.mock.calls.some(([path]) => (path as string).includes("/projects/project-b/documents"))).toBe(false);
  });

  it("preserves other query parameters, the hash and history state when opening a chat", async () => {
    await mountAt("/workspace?view=wide&filter=two%20words&chat=chat-a#sources");
    const projectB = Array.from(container.querySelectorAll(".nav-item")).find((element) => element.textContent?.startsWith("Project B"));
    await click(projectB ?? null);
    expect(new URLSearchParams(window.location.search).get("chat")).toBe("chat-b");
    expect(new URLSearchParams(window.location.search).get("filter")).toBe("two words");
    expect(new URLSearchParams(window.location.search).get("view")).toBe("wide");
    expect(window.location.pathname).toBe("/workspace");
    expect(window.location.hash).toBe("#sources");
    expect(window.history.state).toEqual({ previous: "kept" });
    expect(serverWorkspace.active_chat_id).toBe("chat-b");
    expect(messageRequests().slice(-1)[0]).toBe("/api/chats/chat-b/messages");
  });

  it("updates the tab URL when creating a free chat and clears it on deletion", async () => {
    await mountAt("/?chat=chat-a&view=wide#sources");
    await click(container.querySelector(".new-thread"));
    expect(new URLSearchParams(window.location.search).get("chat")).toBe("new-free");
    expect(serverWorkspace.active_project_id).toBeNull();

    await openMenu(container.querySelector(".chat-nav-item.active"));
    await click(document.querySelector(".nav-hover-menu .danger"));
    expect(new URLSearchParams(window.location.search).has("chat")).toBe(false);
    expect(window.location.search).toBe("?view=wide");
    expect(window.location.hash).toBe("#sources");
    expect(serverWorkspace.active_chat_id).toBe("");
    expect(container.querySelector('[aria-label="Документы проекта"]')).toBeNull();
  });

  it("updates the tab URL when creating a project chat", async () => {
    await mountAt("/?chat=chat-a");
    const projectA = Array.from(container.querySelectorAll(".nav-item")).find((element) => element.textContent?.startsWith("Project A"));
    await openMenu(projectA ?? null);
    const createButton = Array.from(document.querySelectorAll(".nav-menu-action")).find((element) => element.textContent === "Новый чат");
    await click(createButton ?? null);
    expect(window.location.search).toBe("?chat=new-project-chat");
    expect(serverWorkspace.active_project_id).toBe("project-a");
  });

  it("clears an unavailable URL chat when the account has no chats", async () => {
    availableChats = [];
    await mountAt("/?chat=foreign-chat&view=wide#sources");
    expect(messageRequests()).toEqual([]);
    expect(window.location.search).toBe("?view=wide");
    expect(window.location.hash).toBe("#sources");
  });
});
