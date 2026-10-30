import { act, createElement } from "react";
import { useExternalStoreRuntime, type AssistantRuntime, type ThreadMessage } from "@assistant-ui/react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { withStableThreadListSnapshot } from "./runtimeSnapshot";

// Deliberately use the real @assistant-ui/react runtime and message primitives.
// Mock only HTTP and browser APIs unavailable in jsdom; never call a model/server.
describe("persisted answers with the real assistant runtime", () => {
  const chatId = "synthetic-chat";
  const messageId = "synthetic-answer";
  const jobId = "synthetic-job";
  const question = "Синтетический вопрос для проверки истории";
  const answer = "Сохранённый синтетический ответ с исходными фрагментами.";
  const quote = "ТОЧНЫЙ ИСХОДНЫЙ ФРАГМЕНТ\nУсловия сохраняются целиком.";
  const fetchMock = vi.fn();
  let sourceScore: number | null | undefined;
  let root: Root | undefined;
  let container: HTMLDivElement;
  let originalScrollIntoView: PropertyDescriptor | undefined;
  let originalScrollTo: PropertyDescriptor | undefined;

  function jsonResponse(value: unknown) {
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      headers: new Headers({ "content-type": "application/json" }),
      json: async () => JSON.parse(JSON.stringify(value))
    };
  }

  async function mount() {
    window.history.replaceState(null, "", `/?chat=${chatId}`);
    root = createRoot(container);
    await act(async () => root!.render(createElement(App)));
    // Flush the real runtime's scheduled resource work, not just React effects.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }

  async function unmount() {
    await act(async () => root?.unmount());
    root = undefined;
  }

  async function click(element: Element | null) {
    expect(element).not.toBeNull();
    await act(async () => (element as HTMLElement).click());
  }

  async function openDebug() {
    await click(Array.from(container.querySelectorAll(".panel-tabs button"))
      .find((button) => button.textContent === "Debug") ?? null);
  }

  function jobRequests() {
    return fetchMock.mock.calls.filter(([path]) => path === `/api/jobs/${jobId}`);
  }

  function expectVisibleMessages() {
    expect(container.querySelectorAll(".message-row")).toHaveLength(2);
    expect(container.querySelector(".message-row.user")?.textContent).toContain(question);
    expect(container.querySelector(".message-row.assistant")?.textContent).toContain(answer);
  }

  beforeEach(() => {
    sourceScore = 0.875;
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    vi.stubGlobal("IntersectionObserver", class {
      root = null;
      rootMargin = "0px";
      thresholds = [0];
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() { return []; }
    });
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: false, media: query, onchange: null,
      addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
      dispatchEvent() { return false; }
    }));
    originalScrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
    originalScrollTo = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTo");
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
    Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
    window.localStorage.clear();
    container = document.createElement("div");
    document.body.appendChild(container);
    const routes: Record<string, unknown> = {
      "/api/auth/session": {
        authenticated: true, csrf_token: "synthetic-only",
        user: { id: "synthetic-user", email: "qa@example.test", display_name: "Synthetic QA", is_admin: false, can_manage_global_documents: false }
      },
      "/api/workspace": { active_chat_id: chatId, active_project_id: null },
      "/api/projects": { items: [] },
      "/api/chats": { items: [{ id: chatId, project_id: null, title: "Synthetic runtime QA" }] },
      [`/api/chats/${chatId}/messages`]: { items: [
        { id: "synthetic-question", chat_id: chatId, role: "user", content: question, status: "complete" },
        { id: messageId, chat_id: chatId, role: "assistant", content: answer, status: "complete", metadata: { job_id: jobId, sources: [], formulas: [], warnings: [] } }
      ] },
      [`/api/jobs/${jobId}`]: {
        id: jobId, kind: "generation", status: "completed",
        result: { job_id: jobId, message_id: messageId, status: "complete", context: [
          { file: "synthetic.txt", text: quote, score: 0.875, section_path: "Исходный раздел", flow_origin: "python" }
        ] }
      },
      "/api/models": { models: ["synthetic-model"], selected: "synthetic-model" },
      "/api/providers": { config: { provider: "ollama", model: "synthetic-model" }, defaults: {} },
      "/api/system-prompts": { prompts: [], selected: "rag-grounded" },
      "/api/flow-mode": { mode: "python" },
      "/api/documents": { items: [] }
    };
    fetchMock.mockImplementation(async (path: string, options?: RequestInit) => {
      if ((options?.method ?? "GET") !== "GET") throw new Error(`Unexpected mutation in read-only fixture: ${path}`);
      const pathname = new URL(path, window.location.origin).pathname;
      if (!(pathname in routes)) throw new Error(`Unexpected fixture request: ${path}`);
      const response = jsonResponse(routes[pathname]);
      if (pathname === `/api/jobs/${jobId}`) {
        const job = await response.json();
        if (sourceScore === undefined) delete job.result.context[0].score;
        else job.result.context[0].score = sourceScore;
        return jsonResponse(job);
      }
      return response;
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(async () => {
    await unmount();
    container.remove();
    if (originalScrollIntoView) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", originalScrollIntoView);
    else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
    if (originalScrollTo) Object.defineProperty(HTMLElement.prototype, "scrollTo", originalScrollTo);
    else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollTo;
    fetchMock.mockReset();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
    window.localStorage.clear();
  });

  it("confirms the installed bridge allocates equivalent disconnected snapshots and adapts their identity", async () => {
    let captured: AssistantRuntime | undefined;
    function RuntimeProbe() {
      captured = useExternalStoreRuntime<ThreadMessage>({ messages: [], onNew: async () => {} });
      return null;
    }
    root = createRoot(container);
    await act(async () => root!.render(createElement(RuntimeProbe)));
    const runtime = captured!;
    const first = runtime.threads.getState();
    const second = runtime.threads.getState();
    expect(second).toEqual(first);
    expect(second).not.toBe(first);
    const adapted = withStableThreadListSnapshot(runtime);
    expect(adapted.threads.getState()).toBe(adapted.threads.getState());
  });

  it("renders stored messages and recovers their exact Debug snapshot after a real remount without retrieval", async () => {
    await mount();
    expectVisibleMessages();
    expect(jobRequests()).toHaveLength(0);
    await openDebug();
    expect(container.querySelector(".context-text")).toBeNull();
    await click(container.querySelector(".debug-button"));
    expect(container.querySelector(".context-text")?.textContent).toBe(quote);
    expect(container.querySelector(".source-meta")?.textContent).toContain("score=0.875");
    expectVisibleMessages();
    expect(jobRequests()).toHaveLength(1);
    await click(container.querySelector(".debug-button"));
    expect(jobRequests()).toHaveLength(1);

    await unmount();
    await mount();
    expectVisibleMessages();
    await openDebug();
    expect(container.querySelector(".context-text")).toBeNull();
    await click(container.querySelector(".debug-button"));
    expect(container.querySelector(".context-text")?.textContent).toBe(quote);
    expect(jobRequests()).toHaveLength(2);
    expect(fetchMock.mock.calls.some(([path]) => path === "/api/debug" || path === "/api/ask")).toBe(false);
  });

  it.each([undefined, null])("recovers canonical unranked sections and displays a dash (score=%s)", async (score) => {
    sourceScore = score;
    await mount();
    await openDebug();
    await click(container.querySelector(".debug-button"));
    expect(container.querySelector(".context-text")?.textContent).toBe(quote);
    expect(container.querySelector(".source-meta")?.textContent).toContain("score=—");
    expectVisibleMessages();
    expect(jobRequests()).toHaveLength(1);
    expect(fetchMock.mock.calls.some(([path]) => path === "/api/debug" || path === "/api/ask")).toBe(false);
  });
});
