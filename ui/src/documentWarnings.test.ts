import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

// Only the viewport runtime is replaced. Document rows, project memory, scope
// requests and UI interactions use the actual App. No server/model is called.
vi.mock("@assistant-ui/react", async () => {
  const { createElement } = await import("react");
  return {
    AssistantRuntimeProvider: ({ children }: { children?: import("react").ReactNode }) => createElement("div", null, children),
    useExternalStoreRuntime: vi.fn(() => ({ threads: { getState: () => ({}) } })),
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

describe("document processing warnings across scopes and project memory", () => {
  let root: Root | undefined;
  let container: HTMLDivElement;
  const fetchMock = vi.fn();
  const documents = [
    { id: "global-table", file_name: "Общая таблица без изображений.docx", scope: "global", status: "ready", metadata: { warnings: ["table-synthetic: no-explicit-header"], parsing_backend: "xml" } },
    { id: "project-table", file_name: "Таблица проекта без изображений.docx", scope: "project", project_id: "p", status: "ready", metadata: { warnings: ["table-synthetic: no-explicit-header"], parsing_backend: "xml" } },
    { id: "chat-table", file_name: "Таблица чата без изображений.docx", scope: "project", project_id: "p", chat_id: "c", status: "ready", metadata: { warnings: ["table-synthetic: no-explicit-header"], parsing_backend: "xml" } },
    { id: "clean", file_name: "Документ без замечаний.md", scope: "project", project_id: "p", status: "ready", metadata: { warnings: [] } },
    { id: "queued", file_name: "Ожидающий документ.md", scope: "project", project_id: "p", status: "queued", metadata: { warnings: ["table-synthetic: no-explicit-header"] } },
    { id: "processing", file_name: "Обрабатываемый документ.md", scope: "project", project_id: "p", status: "processing", processing_state: { progress: 27 } },
    { id: "empty-warning", file_name: "Пустое предупреждение.md", scope: "project", project_id: "p", status: "ready", metadata: { warnings: ["", "   "] } },
    { id: "invalid-warning", file_name: "Некорректное поле предупреждений.md", scope: "project", project_id: "p", status: "ready", metadata: { warnings: "not-an-array" } }
  ];

  function jsonResponse(value: unknown) {
    return { ok: true, status: 200, statusText: "OK", headers: new Headers({ "content-type": "application/json" }), json: async () => value };
  }

  async function mount() {
    window.history.replaceState(null, "", "/?chat=c");
    root = createRoot(container);
    await act(async () => root!.render(createElement(App)));
  }

  function link(id: string, parent: ParentNode = container) {
    const result = parent.querySelector<HTMLAnchorElement>(`a[href="/api/documents/${id}/content"]`);
    expect(result).not.toBeNull();
    return result!;
  }

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    window.localStorage.clear();
    container = document.createElement("div");document.body.appendChild(container);
    fetchMock.mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === "/api/auth/session") return jsonResponse({ authenticated: true, csrf_token: "synthetic-only", user: { id: "u", email: "qa@example.test", display_name: "Synthetic", is_admin: false, can_manage_global_documents: false } });
      if (path === "/api/workspace" || path === "/api/settings") return jsonResponse({ active_project_id: "p", active_chat_id: "c" });
      if ((options?.method ?? "GET") !== "GET") throw new Error(`Unexpected mutation ${path}`);
      if (path === "/api/projects") return jsonResponse({ items: [{ id: "p", name: "Synthetic Project", knowledge_scope: "project_and_global" }] });
      if (path === "/api/chats") return jsonResponse({ items: [{ id: "c", project_id: "p", title: "Synthetic chat" }] });
      if (path === "/api/chats/c/messages") return jsonResponse({ items: [] });
      if (path === "/api/models") return jsonResponse({ models: ["synthetic-model"], selected: "synthetic-model" });
      if (path === "/api/providers") return jsonResponse({ config: { provider: "ollama", model: "synthetic-model" }, defaults: {} });
      if (path === "/api/system-prompts") return jsonResponse({ prompts: [], selected: "rag-grounded" });
      if (path === "/api/flow-mode") return jsonResponse({ mode: "python" });
      if (path === "/api/documents") return jsonResponse({ items: documents.filter((row) => row.scope === "global") });
      if (path === "/api/projects/p/documents") return jsonResponse({ items: documents.filter((row) => row.scope === "project" && !("chat_id" in row)) });
      if (path === "/api/documents?chat_id=c") return jsonResponse({ items: documents.filter((row) => "chat_id" in row) });
      throw new Error(`Unexpected fixture request ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(async () => {
    await act(async () => root?.unmount());root=undefined;
    container.remove();fetchMock.mockReset();vi.restoreAllMocks();vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");window.localStorage.clear();
  });

  it.each(["global-table", "project-table", "chat-table"])("shows generic truthful warning help for ready %s without claiming image recognition", async (id) => {
    await mount();
    const row=link(id);
    expect(row.title).toContain("предупреждения обработки");
    expect(row.title).not.toContain("Часть изображений распознана");
    expect(row.title).not.toContain("распознана с предупреждениями");
    expect(row.querySelector(".nav-document-status")?.textContent).toBe("Готов ");
    expect(row.querySelector('[role="img"][aria-label="Есть предупреждения обработки"]')).not.toBeNull();
  });

  it("shows the same warning badge and help for the same document in project memory", async () => {
    await mount();
    const sidebar=link("project-table");
    const projectButton=container.querySelector<HTMLButtonElement>(".project-tree .nav-item");
    expect(projectButton).not.toBeNull();
    await act(async () => projectButton!.focus());
    const memory=Array.from(document.querySelectorAll(".nav-menu-action")).find((button) => button.textContent === "Память и документы");
    expect(memory).toBeDefined();
    await act(async () => (memory as HTMLElement).click());
    const dialog=document.querySelector('[role="dialog"]');expect(dialog).not.toBeNull();
    const project=link("project-table",dialog!);
    expect(project.title).toContain("предупреждения обработки");
    expect(project.title).toBe(sidebar.title);
    expect(project.querySelector('[role="img"][aria-label="Есть предупреждения обработки"]')).not.toBeNull();
    expect(project.querySelector(".nav-document-status")?.getAttribute("title"))
      .toBe(sidebar.querySelector(".nav-document-status")?.getAttribute("title"));
  });

  it("keeps actual queued/processing/clean-ready status and ignores empty or malformed warnings", async () => {
    await mount();
    expect(link("queued").querySelector(".nav-document-status")?.textContent).toBe("queued");
    expect(link("processing").querySelector(".nav-document-status")?.textContent).toBe("27%");
    for (const id of ["clean", "empty-warning", "invalid-warning"]) {
      expect(link(id).querySelector(".nav-document-status")?.textContent).toBe("Готов");
      expect(link(id).querySelector('[role="img"]')).toBeNull();
      expect(link(id).title).not.toContain("предупреждения");
    }
    expect(link("queued").querySelector('[role="img"]')).toBeNull();
  });
});
