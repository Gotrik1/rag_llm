import {
  ActionBarPrimitive,
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useExternalStoreRuntime,
  useMessage,
  type AppendMessage,
  type ExternalStoreAdapter,
  type ThreadMessageLike
} from "@assistant-ui/react";
import {
  AlertTriangle,
  Bot,
  Bug,
  CheckCircle2,
  Copy,
  Database,
  Cpu,
  FileText,
  Files,
  PanelRight,
  RefreshCw,
  RotateCcw,
  Save,
  Send,
  Sigma,
  Sparkles,
  TestTube2,
  UploadCloud,
  User
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type Role = "user" | "assistant";

type ChatEntry = {
  id: string;
  role: Role;
  text: string;
  html?: string;
  modelLabel?: string;
  status?: "running" | "complete" | "error";
};

type WorkspaceState = {
  id: string;
  active_project_id?: string | null;
  active_chat_id?: string | null;
  settings?: Record<string, unknown>;
};

type ProjectRecord = {
  id: string;
  name: string;
  description: string;
  settings?: Record<string, unknown>;
  memory?: Record<string, unknown>;
  chats?: ChatRecord[];
};

type ChatRecord = {
  id: string;
  project_id: string;
  title: string;
  mode: FlowMode;
  provider: ProviderName;
  model_name: string;
  metadata?: Record<string, unknown>;
};

type MessageRecord = {
  id: string;
  chat_id: string;
  role: Role;
  content: string;
  content_html?: string;
  status?: string;
  metadata?: Record<string, unknown>;
};

type ApiSource = {
  file: string;
  section?: string;
  score: number;
  text_preview?: string;
  html_preview?: string;
};

type ApiFormula = {
  text: string;
  html?: string;
};

type ApiContextChunk = {
  file: string;
  section_path?: string;
  chunk_type?: string;
  status?: string;
  score: number;
  text: string;
  html_text?: string;
  flow_origin?: "python" | "rust";
};

type AskResponse = {
  answer: string;
  html_answer?: string;
  from_cache: boolean;
  elapsed_s: number;
  usage?: {
    total_tokens_est?: number;
    context_tokens_est?: number;
    answer_tokens_est?: number;
  };
  sources?: ApiSource[];
  formulas?: ApiFormula[];
  context?: ApiContextChunk[];
  warnings?: string[];
  llm?: { provider: string; model: string; label: string };
};

type DebugResponse = {
  formulas?: ApiFormula[];
  context?: ApiContextChunk[];
  warnings?: string[];
  elapsed_s: number;
  usage?: AskResponse["usage"];
};

type UploadResponse = {
  chunks: number;
  files: {
    file: string;
    stored_path: string;
    chunks: number;
  }[];
};

type ModelsResponse = {
  models: string[];
  selected: string;
};

type ProviderName = "ollama" | "openai" | "deepseek" | "gemini" | "gigachat" | "yandex";

type ProviderConfig = {
  provider: ProviderName;
  model: string;
  base_url?: string;
  folder_id?: string;
  auth_type?: string;
  has_api_key?: boolean;
  api_key_hint?: string;
};

type ProvidersResponse = {
  config: ProviderConfig;
  defaults: Record<string, { base_url: string; model: string }>;
};

type SystemPrompt = {
  id: string;
  name: string;
  description: string;
};

type SystemPromptsResponse = {
  prompts: SystemPrompt[];
  selected: string;
};

type FlowMode = "python" | "rust" | "hybrid";

type PanelTab = "evidence" | "sources" | "debug";

const ASK_TIMEOUT_MS = 180_000;
const DEBUG_TIMEOUT_MS = 60_000;
const UPLOAD_TIMEOUT_MS = 300_000;
const API_TIMEOUT_MS = 60_000;

declare global {
  interface Window {
    MathJax?: {
      typesetPromise?: (elements?: HTMLElement[]) => Promise<void>;
      startup?: {
        typeset?: boolean;
      };
      svg?: {
        fontCache?: string;
      };
    };
  }
}

const createId = () => crypto.randomUUID();

async function postJson<T>(path: string, body: unknown, timeoutMs: number): Promise<T> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal
    });
    const data = (await response.json()) as T & { error?: string };
    if (!response.ok) {
      throw new Error(data.error || response.statusText);
    }
    return data;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("Backend не ответил вовремя. Запрос можно повторить.");
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

async function postFormData<T>(path: string, body: FormData, timeoutMs: number): Promise<T> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(path, {
      method: "POST",
      body,
      signal: controller.signal
    });
    const data = (await response.json()) as T & { error?: string };
    if (!response.ok) {
      throw new Error(data.error || response.statusText);
    }
    return data;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("Загрузка не завершилась вовремя. Попробуй меньший файл или повтори позже.");
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { cache: "no-store" });
  const data = (await response.json()) as T & { error?: string };
  if (!response.ok) {
    throw new Error(data.error || response.statusText);
  }
  return data;
}

const extractText = (message: AppendMessage) => {
  const content = message.content;
  if (typeof content === "string") return content;
  return content
    .map((part) => {
      if (part.type === "text") return part.text;
      return "";
    })
    .join("")
    .trim();
};

const toThreadMessage = (message: ChatEntry): ThreadMessageLike => ({
  id: message.id,
  role: message.role,
  content:
    message.status === "running" && !message.text
      ? "Думаю..."
      : message.text,
  status:
    message.role === "assistant"
      ? message.status === "running"
        ? { type: "running" }
        : message.status === "error"
          ? { type: "incomplete", reason: "error", error: "Backend request failed" }
          : { type: "complete", reason: "stop" }
      : undefined,
  createdAt: new Date(),
  metadata: {
    custom: {
      html: message.html,
      modelLabel: message.modelLabel
    }
  }
});

const ensureMathJax = (() => {
  let promise: Promise<void> | null = null;

  return () => {
    if (typeof window === "undefined") return Promise.resolve();
    if (window.MathJax?.typesetPromise) return Promise.resolve();
    if (promise) return promise;

    window.MathJax = {
      startup: { typeset: false },
      svg: { fontCache: "global" }
    };

    promise = new Promise<void>((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/mml-svg.js";
      script.async = true;
      script.onload = () => resolve();
      script.onerror = () => reject(new Error("MathJax load failed"));
      document.head.appendChild(script);
    });

    return promise;
  };
})();

function useMathTypeset(html?: string) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!html || !ref.current) return;
    const element = ref.current;
    ensureMathJax()
      .then(() => window.MathJax?.typesetPromise?.([element]))
      .catch(() => undefined);
  }, [html]);

  return ref;
}

export function App() {
  const [messages, setMessages] = useState<ChatEntry[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [lastResult, setLastResult] = useState<AskResponse | null>(null);
  const [lastQuestion, setLastQuestion] = useState("");
  const [debugResult, setDebugResult] = useState<DebugResponse | null>(null);
  const [debugError, setDebugError] = useState("");
  const [isDebugging, setIsDebugging] = useState(false);
  const [activeTab, setActiveTab] = useState<PanelTab>("evidence");
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [uploadResult, setUploadResult] = useState<UploadResponse | null>(null);
  const [uploadError, setUploadError] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState("");
  const [modelError, setModelError] = useState("");
  const [isLoadingModels, setIsLoadingModels] = useState(true);
  const [isChangingModel, setIsChangingModel] = useState(false);
  const [activeProvider, setActiveProvider] = useState<ProviderName>("ollama");
  const [workspace, setWorkspace] = useState<WorkspaceState | null>(null);
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [activeProjectId, setActiveProjectId] = useState("");
  const [activeChatId, setActiveChatId] = useState("");
  const [workspaceError, setWorkspaceError] = useState("");
  const [isWorkspaceLoading, setIsWorkspaceLoading] = useState(true);

  const activeProject = projects.find((project) => project.id === activeProjectId) ?? projects[0] ?? null;
  const activeChats = activeProject?.chats ?? [];
  const activeChat = activeChats.find((chat) => chat.id === activeChatId) ?? activeChats[0] ?? null;

  const loadWorkspace = useCallback(async () => {
    setIsWorkspaceLoading(true);
    setWorkspaceError("");
    try {
      const [workspaceData, projectData] = await Promise.all([
        fetchJson<WorkspaceState>("/api/workspace"),
        fetchJson<{ items: ProjectRecord[] }>("/api/projects")
      ]);
      setWorkspace(workspaceData);
      setProjects(projectData.items);
      const nextProjectId = workspaceData.active_project_id || projectData.items[0]?.id || "";
      const nextProject = projectData.items.find((item) => item.id === nextProjectId) ?? projectData.items[0];
      const nextChatId = workspaceData.active_chat_id || nextProject?.chats?.[0]?.id || "";
      setActiveProjectId(nextProject?.id || "");
      setActiveChatId(nextChatId);
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось загрузить workspace");
    } finally {
      setIsWorkspaceLoading(false);
    }
  }, []);

  const refreshChats = useCallback(async (projectId?: string) => {
    const targetProjectId = projectId || activeProjectId;
    if (!targetProjectId) return;
    const data = await fetchJson<{ items: ChatRecord[] }>(`/api/projects/${targetProjectId}/chats`);
    setProjects((current) =>
      current.map((project) => (project.id === targetProjectId ? { ...project, chats: data.items } : project))
    );
  }, [activeProjectId]);

  const loadMessages = useCallback(async (chatId?: string) => {
    const targetChatId = chatId || activeChatId;
    if (!targetChatId) return;
    const data = await fetchJson<{ items: MessageRecord[] }>(`/api/chats/${targetChatId}/messages`);
    setMessages(
      data.items.map((item) => ({
        id: item.id,
        role: item.role,
        text: item.content,
        html: item.content_html || undefined,
        status: item.role === "assistant" ? (item.status === "running" ? "running" : item.status === "error" ? "error" : "complete") : "complete",
        modelLabel: item.metadata?.model_label ? String(item.metadata.model_label) : undefined
      }))
    );
  }, [activeChatId]);

  const syncWorkspace = useCallback(async (nextProjectId: string, nextChatId: string) => {
    const updated = await postJson<WorkspaceState>("/api/settings", {
      active_project_id: nextProjectId,
      active_chat_id: nextChatId,
      settings: workspace?.settings ?? {}
    }, API_TIMEOUT_MS);
    setWorkspace(updated);
  }, [workspace?.settings]);

  const persistMessage = useCallback(async (chatId: string, payload: {
    role: Role;
    content: string;
    content_html?: string;
    status?: string;
    metadata?: Record<string, unknown>;
  }) => {
    await postJson<MessageRecord>(`/api/chats/${chatId}/messages`, payload, API_TIMEOUT_MS);
  }, []);

  useEffect(() => {
    if (!activeChatId) return;
    void loadMessages(activeChatId);
  }, [activeChatId, loadMessages]);

  useEffect(() => {
    if (!activeProjectId) return;
    void refreshChats(activeProjectId);
  }, [activeProjectId, refreshChats]);

  const loadModels = useCallback(async () => {
    setIsLoadingModels(true);
    setModelError("");
    try {
      const data = await fetchJson<ModelsResponse>("/api/models");
      setModels(data.models);
      setSelectedModel(data.selected);
    } catch (error) {
      setModelError(error instanceof Error ? error.message : "Не удалось получить список моделей");
    } finally {
      setIsLoadingModels(false);
    }
  }, []);

  useEffect(() => {
    void loadModels();
    void loadWorkspace();
  }, [loadModels, loadWorkspace]);

  const changeModel = useCallback(async (model: string) => {
    if (!model || model === selectedModel || isChangingModel || isRunning) return;
    setIsChangingModel(true);
    setModelError("");
    try {
      const data = await postJson<{ model: string }>("/api/models/select", { model }, DEBUG_TIMEOUT_MS);
      setSelectedModel(data.model);
    } catch (error) {
      setModelError(error instanceof Error ? error.message : "Не удалось сменить модель");
    } finally {
      setIsChangingModel(false);
    }
  }, [isChangingModel, isRunning, selectedModel]);

  const switchProject = useCallback(async (projectId: string) => {
    const project = projects.find((item) => item.id === projectId);
    if (!project) return;
    const chatId = project.chats?.[0]?.id || "";
    setActiveProjectId(projectId);
    setActiveChatId(chatId);
    await syncWorkspace(projectId, chatId);
    if (chatId) {
      await loadMessages(chatId);
    } else {
      setMessages([]);
    }
  }, [loadMessages, projects, syncWorkspace]);

  const switchChat = useCallback(async (chatId: string) => {
    if (!chatId) return;
    setActiveChatId(chatId);
    await syncWorkspace(activeProjectId, chatId);
    await loadMessages(chatId);
  }, [activeProjectId, loadMessages, syncWorkspace]);

  const createProjectRecord = useCallback(async () => {
    const created = await postJson<ProjectRecord>("/api/projects", { name: "Новый проект" }, API_TIMEOUT_MS);
    const refreshed = await fetchJson<{ items: ProjectRecord[] }>("/api/projects");
    setProjects(refreshed.items);
    setActiveProjectId(created.id);
    const chat = await postJson<ChatRecord>(`/api/projects/${created.id}/chats`, { title: "Новый чат" }, API_TIMEOUT_MS);
    await refreshChats(created.id);
    setActiveChatId(chat.id);
    await syncWorkspace(created.id, chat.id);
    setMessages([]);
  }, [refreshChats, syncWorkspace]);

  const createChatRecord = useCallback(async () => {
    if (!activeProjectId) return;
    const chat = await postJson<ChatRecord>(`/api/projects/${activeProjectId}/chats`, { title: "Новый чат" }, API_TIMEOUT_MS);
    await refreshChats(activeProjectId);
    setActiveChatId(chat.id);
    await syncWorkspace(activeProjectId, chat.id);
    setMessages([]);
  }, [activeProjectId, refreshChats, syncWorkspace]);

  const onNew = useCallback(async (message: AppendMessage) => {
    const question = extractText(message);
    if (!question) return;
    if (!activeChatId) return;

    const userMessage: ChatEntry = {
      id: createId(),
      role: "user",
      text: question,
      status: "complete"
    };
    const assistantId = createId();
    const assistantMessage: ChatEntry = {
      id: assistantId,
      role: "assistant",
      text: "",
      status: "running"
    };

    setMessages((current) => [...current, userMessage, assistantMessage]);
    setIsRunning(true);
    setLastResult(null);
    setLastQuestion(question);
    setDebugResult(null);
    setDebugError("");
    setActiveTab("evidence");

    try {
      await persistMessage(activeChatId, { role: "user", content: question });
      const data = await postJson<AskResponse>("/api/ask", { question }, ASK_TIMEOUT_MS);
      await persistMessage(activeChatId, {
        role: "assistant",
        content: data.answer || "Пустой ответ.",
        content_html: data.html_answer || "",
        status: "complete",
        metadata: { llm: data.llm, sources: data.sources, usage: data.usage }
      });

      setLastResult(data);
      setMessages((current) =>
        current.map((item) =>
          item.id === assistantId
            ? {
                ...item,
                text: data.answer || "Пустой ответ.",
                html: data.html_answer,
                modelLabel: data.llm?.label,
                status: "complete"
              }
            : item
        )
      );
    } catch (error) {
      const text = error instanceof Error ? error.message : "Ошибка запроса";
      void persistMessage(activeChatId, { role: "assistant", content: text, status: "error" });
      setMessages((current) =>
        current.map((item) =>
          item.id === assistantId
            ? {
                ...item,
                text,
                status: "error"
              }
            : item
        )
      );
    } finally {
      setIsRunning(false);
    }
  }, [activeChatId, persistMessage]);

  const adapter = useMemo<ExternalStoreAdapter<ChatEntry>>(
    () => ({
      messages,
      isRunning,
      onNew,
      setMessages: (next) => setMessages([...next]),
      convertMessage: toThreadMessage
    }),
    [messages, isRunning, onNew]
  );

  const runtime = useExternalStoreRuntime(adapter);

  const clearThread = async () => {
    await createChatRecord();
    setMessages([]);
    setLastResult(null);
    setLastQuestion("");
    setDebugResult(null);
    setDebugError("");
  };

  const runDebug = useCallback(async () => {
    if (!lastQuestion || isRunning || isDebugging) return;
    setIsDebugging(true);
    setDebugError("");
    setActiveTab("debug");

    try {
      const data = await postJson<DebugResponse>("/api/debug", { question: lastQuestion }, DEBUG_TIMEOUT_MS);
      setDebugResult(data);
    } catch (error) {
      setDebugError(error instanceof Error ? error.message : "Ошибка debug-запроса");
    } finally {
      setIsDebugging(false);
    }
  }, [isDebugging, isRunning, lastQuestion]);

  const uploadFiles = useCallback(async () => {
    if (!selectedFiles.length || isUploading) return;
    const form = new FormData();
    selectedFiles.forEach((file) => form.append("files", file));

    setIsUploading(true);
    setUploadError("");
    setUploadResult(null);

    try {
      const data = await postFormData<UploadResponse>("/api/upload", form, UPLOAD_TIMEOUT_MS);
      setUploadResult(data);
      setSelectedFiles([]);
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "Ошибка загрузки файлов");
    } finally {
      setIsUploading(false);
    }
  }, [isUploading, selectedFiles]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <main className="app-shell">
        <aside className="sidebar">
          <div className="brand">
            <div className="brand-mark">
              <Sparkles size={18} />
            </div>
            <div>
              <div className="brand-title">RAG Assistant</div>
              <div className="brand-subtitle">Ollama + Qdrant</div>
            </div>
          </div>
          <button className="new-thread" onClick={() => void clearThread()} type="button">
            <RotateCcw size={16} />
            Новый диалог
          </button>
          <section className="workspace-switcher" aria-label="Workspace">
            <div className="loader-title">
              <Database size={15} />
              <span>Workspace</span>
            </div>
            <select disabled={isWorkspaceLoading || !projects.length} value={activeProjectId} onChange={(event) => void switchProject(event.target.value)}>
              {!projects.length ? <option value="">Нет проектов</option> : null}
              {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
            </select>
            <select disabled={isWorkspaceLoading || !activeChats.length} value={activeChatId} onChange={(event) => void switchChat(event.target.value)}>
              {!activeChats.length ? <option value="">Нет чатов</option> : null}
              {activeChats.map((chat) => <option key={chat.id} value={chat.id}>{chat.title}</option>)}
            </select>
            <div className="provider-actions">
              <button type="button" onClick={() => void createProjectRecord()}><Files size={14} /> Проект</button>
              <button type="button" onClick={() => void createChatRecord()}><Sparkles size={14} /> Чат</button>
            </div>
            {workspaceError ? <div className="provider-status">{workspaceError}</div> : null}
            {workspace ? <div className="sidebar-note">Активный проект: {activeProject?.name ?? "нет"}</div> : null}
          </section>
          <ProviderSettings models={models} onProviderChange={setActiveProvider} />
          <FlowModeSelector isRunning={isRunning} />
          <SystemPromptSelector isRunning={isRunning} />
          {activeProvider === "ollama" ? (
            <ModelSelector
              error={modelError}
              isChanging={isChangingModel}
              isLoading={isLoadingModels}
              isRunning={isRunning}
              models={models}
              selectedModel={selectedModel}
              onChange={changeModel}
              onRefresh={loadModels}
            />
          ) : null}
          <KnowledgeLoader
            error={uploadError}
            isUploading={isUploading}
            result={uploadResult}
            selectedFiles={selectedFiles}
            onFilesChange={setSelectedFiles}
            onUpload={uploadFiles}
          />
          <div className="sidebar-note">
            Интерфейс работает через assistant-ui и локальный backend
            127.0.0.1:8080.
          </div>
        </aside>

        <section className="chat-surface">
          <Thread />
        </section>

        <EvidencePanel
          activeTab={activeTab}
          debugError={debugError}
          debugResult={debugResult}
          isDebugging={isDebugging}
          lastQuestion={lastQuestion}
          result={lastResult}
          onDebug={runDebug}
          onTabChange={setActiveTab}
        />
      </main>
    </AssistantRuntimeProvider>
  );
}

function FlowModeSelector({ isRunning }: { isRunning: boolean }) {
  const [mode, setMode] = useState<FlowMode>("python");
  const [status, setStatus] = useState("");

  useEffect(() => {
    fetch("/api/flow-mode", { cache: "no-store" })
      .then(async (response) => {
        const data = (await response.json()) as { mode?: FlowMode; error?: string };
        if (!response.ok) throw new Error(data.error || response.statusText);
        if (data.mode) setMode(data.mode);
      })
      .catch((error: unknown) => setStatus(error instanceof Error ? error.message : "Не удалось загрузить режим flow"));
  }, []);

  const change = async (next: FlowMode) => {
    if (next === mode || isRunning) return;
    setStatus("");
    try {
      const data = await postJson<{ mode: FlowMode; cache_cleared?: boolean }>("/api/flow-mode", { mode: next }, DEBUG_TIMEOUT_MS);
      setMode(data.mode);
      setStatus(data.cache_cleared ? "Режим переключён, кэш ответов очищен." : "Выбран для следующих запросов.");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Не удалось переключить flow");
    }
  };

  return (
    <section className="flow-mode-selector" aria-label="Выбор flow">
      <div className="loader-title"><Cpu size={15} /><span>Режим обработки</span></div>
      <select disabled={isRunning} value={mode} onChange={(event) => void change(event.target.value as FlowMode)}>
        <option value="python">1. Использовать Python</option>
        <option value="rust">2. Использовать Rust</option>
        <option value="hybrid">3. Гибрид: Rust + Python</option>
      </select>
      <div className="prompt-description">{mode === "hybrid" ? "Rust ищет контекст, Python формирует и проверяет ответ." : mode === "rust" ? "Полный Rust flow: retrieval и генерация." : "Текущий проверенный Python flow."}</div>
      {status ? <div className="provider-status">{status}</div> : null}
    </section>
  );
}

function SystemPromptSelector({ isRunning }: { isRunning: boolean }) {
  const [prompts, setPrompts] = useState<SystemPrompt[]>([]);
  const [selected, setSelected] = useState("rag-grounded");
  const [status, setStatus] = useState("");

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/system-prompts", { cache: "no-store" });
      const data = (await response.json()) as SystemPromptsResponse & { error?: string };
      if (!response.ok) throw new Error(data.error || response.statusText);
      setPrompts(data.prompts);
      setSelected(data.selected);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Не удалось загрузить системные промты");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const change = async (promptId: string) => {
    if (promptId === selected || isRunning) return;
    setStatus("");
    try {
      const data = await postJson<{ selected: string }>("/api/system-prompts/select", { prompt_id: promptId }, DEBUG_TIMEOUT_MS);
      setSelected(data.selected);
      setStatus("Выбран для следующих запросов.");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Не удалось переключить промт");
    }
  };

  const active = prompts.find((prompt) => prompt.id === selected);
  return (
    <section className="system-prompt-selector" aria-label="Выбор системного промта">
      <div className="loader-title"><Sparkles size={15} /><span>Системный промт</span></div>
      <select disabled={isRunning || !prompts.length} value={selected} onChange={(event) => void change(event.target.value)}>
        {prompts.map((prompt) => <option key={prompt.id} value={prompt.id}>{prompt.name}</option>)}
      </select>
      {active ? <div className="prompt-description">{active.description}</div> : null}
      {status ? <div className="provider-status">{status}</div> : null}
    </section>
  );
}

function ProviderSettings({
  models,
  onProviderChange
}: {
  models: string[];
  onProviderChange: (provider: ProviderName) => void;
}) {
  const [config, setConfig] = useState<ProviderConfig>({ provider: "ollama", model: "" });
  const [defaults, setDefaults] = useState<ProvidersResponse["defaults"]>({});
  const [apiKey, setApiKey] = useState("");
  const [status, setStatus] = useState("");
  const [isSaving, setIsSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/providers", { cache: "no-store" });
      const data = (await response.json()) as ProvidersResponse & { error?: string };
      if (!response.ok) throw new Error(data.error || response.statusText);
      setConfig(data.config);
      setDefaults(data.defaults);
      onProviderChange(data.config.provider);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Не удалось загрузить настройки");
    }
  }, [onProviderChange]);

  useEffect(() => { void load(); }, [load]);

  const changeProvider = (provider: ProviderName) => {
    const preset = defaults[provider];
    setConfig((current) => ({
      ...current,
      provider,
      model: provider === "ollama" ? (models[0] ?? "") : (preset?.model ?? current.model),
      base_url: preset?.base_url ?? current.base_url,
      folder_id: "",
      auth_type: "api_key"
    }));
    setApiKey("");
    setStatus("");
  };

  const payload = () => ({ ...config, api_key: apiKey });
  const save = async (test = false) => {
    setIsSaving(true);
    setStatus("");
    try {
      const path = test ? "/api/providers/test" : "/api/providers";
      const result = await postJson<{ config?: ProviderConfig; message?: string }>(path, payload(), 180_000);
      if (result.config) {
        setConfig(result.config);
        setApiKey("");
        onProviderChange(result.config.provider);
        setStatus("Сохранено. Ответы будут генерироваться выбранным провайдером.");
      } else {
        setStatus(result.message ?? "Подключение успешно.");
      }
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Ошибка подключения");
    } finally {
      setIsSaving(false);
    }
  };

  const isYandex = config.provider === "yandex";
  const needsKey = config.provider !== "ollama";
  return (
    <details className="provider-settings">
      <summary>Подключение LLM</summary>
      <div className="provider-fields">
        <label>Провайдер
          <select value={config.provider} onChange={(event) => changeProvider(event.target.value as ProviderName)}>
            <option value="ollama">Ollama (локально)</option>
            <option value="openai">ChatGPT / OpenAI</option>
            <option value="deepseek">DeepSeek</option>
            <option value="gemini">Google Gemini</option>
            <option value="gigachat">GigaChat</option>
            <option value="yandex">YandexGPT</option>
          </select>
        </label>
        <label>Модель
          {config.provider === "ollama" ? (
            <select value={config.model} onChange={(event) => setConfig({ ...config, model: event.target.value })}>
              {!models.length ? <option value="">Нет доступных моделей</option> : null}
              {models.map((model) => <option key={model} value={model}>{model}</option>)}
            </select>
          ) : (
            <input value={config.model} onChange={(event) => setConfig({ ...config, model: event.target.value })} />
          )}
        </label>
        {config.provider !== "ollama" ? <label>Base URL<input value={config.base_url ?? ""} onChange={(event) => setConfig({ ...config, base_url: event.target.value })} /></label> : null}
        {isYandex ? <label>Folder ID<input value={config.folder_id ?? ""} onChange={(event) => setConfig({ ...config, folder_id: event.target.value })} /></label> : null}
        {isYandex ? <label>Тип ключа<select value={config.auth_type ?? "api_key"} onChange={(event) => setConfig({ ...config, auth_type: event.target.value })}><option value="api_key">API key</option><option value="iam">IAM token</option></select></label> : null}
        {needsKey ? <label>API-ключ<input placeholder={config.api_key_hint || "Введите ключ"} type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} /></label> : null}
        <div className="provider-actions">
          <button disabled={isSaving} onClick={() => void save(true)} type="button"><TestTube2 size={14} /> Проверить</button>
          <button className="provider-save" disabled={isSaving} onClick={() => void save(false)} type="button"><Save size={14} /> Сохранить</button>
        </div>
        {status ? <div className="provider-status">{status}</div> : null}
      </div>
    </details>
  );
}

function ModelSelector({
  error,
  isChanging,
  isLoading,
  isRunning,
  models,
  selectedModel,
  onChange,
  onRefresh
}: {
  error: string;
  isChanging: boolean;
  isLoading: boolean;
  isRunning: boolean;
  models: string[];
  selectedModel: string;
  onChange: (model: string) => void;
  onRefresh: () => void;
}) {
  return (
    <section className="model-selector" aria-label="Выбор LLM">
      <div className="loader-title">
        <Cpu size={15} />
        <span>Модель LLM</span>
        <button className="refresh-models" disabled={isLoading || isChanging} onClick={onRefresh} title="Обновить список" type="button">
          <RefreshCw size={14} className={isLoading ? "spin" : ""} />
        </button>
      </div>
      <select
        aria-label="Выбранная модель"
        className="model-select"
        disabled={isLoading || isChanging || isRunning || !models.length}
        onChange={(event) => onChange(event.target.value)}
        value={selectedModel}
      >
        {!models.length ? <option value="">{isLoading ? "Загрузка моделей..." : "Модели не найдены"}</option> : null}
        {selectedModel && !models.includes(selectedModel) ? <option value={selectedModel}>{selectedModel} (не найдена)</option> : null}
        {models.map((model) => <option key={model} value={model}>{model}</option>)}
      </select>
      {isChanging ? <div className="loader-muted">Переключение модели…</div> : null}
      {error ? <div className="model-error">{error}</div> : null}
    </section>
  );
}

function KnowledgeLoader({
  error,
  isUploading,
  result,
  selectedFiles,
  onFilesChange,
  onUpload
}: {
  error: string;
  isUploading: boolean;
  result: UploadResponse | null;
  selectedFiles: File[];
  onFilesChange: (files: File[]) => void;
  onUpload: () => void;
}) {
  const inputId = "knowledge-file-input";

  return (
    <section className="knowledge-loader" aria-label="Загрузка в базу знаний">
      <div className="loader-title">
        <Database size={15} />
        <span>База знаний</span>
      </div>
      <label className="file-picker" htmlFor={inputId}>
        <UploadCloud size={17} />
        <span>{selectedFiles.length ? `Выбрано: ${selectedFiles.length}` : "Выбрать файлы"}</span>
      </label>
      <input
        accept=".docx,.pdf,.md,.txt"
        className="file-input"
        id={inputId}
        multiple
        onChange={(event) => onFilesChange(Array.from(event.currentTarget.files ?? []))}
        type="file"
      />

      {selectedFiles.length ? (
        <div className="selected-files">
          {selectedFiles.slice(0, 4).map((file) => (
            <div className="selected-file" key={`${file.name}-${file.size}`}>
              <FileText size={13} />
              <span>{file.name}</span>
            </div>
          ))}
          {selectedFiles.length > 4 ? <div className="loader-muted">и еще {selectedFiles.length - 4}</div> : null}
        </div>
      ) : null}

      <button
        className="upload-button"
        disabled={!selectedFiles.length || isUploading}
        onClick={onUpload}
        type="button"
      >
        <UploadCloud size={15} />
        {isUploading ? "Загрузка..." : "Загрузить"}
      </button>

      {result ? (
        <div className="upload-status success">
          <CheckCircle2 size={14} />
          <span>{result.files.length} файл(ов), {result.chunks} чанков</span>
        </div>
      ) : null}
      {error ? (
        <div className="upload-status error">
          <AlertTriangle size={14} />
          <span>{error}</span>
        </div>
      ) : null}
    </section>
  );
}

function Thread() {
  return (
    <ThreadPrimitive.Root className="thread-root">
      <ThreadPrimitive.Viewport className="thread-viewport">
        <ThreadPrimitive.Empty>
          <div className="empty-state">
            <div className="empty-icon">
              <Bot size={24} />
            </div>
            <h1>Задай вопрос по регламентам</h1>
            <p>
              Можно спрашивать по номеру пункта, названию раздела или терминам
              вроде ИВ0, ИВ1, ИС.
            </p>
          </div>
        </ThreadPrimitive.Empty>

        <ThreadPrimitive.Messages components={{ Message }} />

        <ThreadPrimitive.ViewportFooter className="thread-footer">
          <Composer />
        </ThreadPrimitive.ViewportFooter>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
}

function Message() {
  const role = useMessage((state) => state.role);
  const html = useMessage((state) => state.metadata.custom.html);
  const modelLabel = useMessage((state) => state.metadata.custom.modelLabel);
  const isAssistant = role === "assistant";

  return (
    <MessagePrimitive.Root className={`message-row ${isAssistant ? "assistant" : "user"}`}>
      <div className="message-avatar">
        {isAssistant ? <Bot size={16} /> : <User size={16} />}
      </div>
      <div className="message-body">
        {isAssistant && typeof modelLabel === "string" && modelLabel ? (
          <div className="model-badge">Ответил: {modelLabel}</div>
        ) : null}
        {isAssistant && typeof html === "string" && html ? (
          <HtmlBlock className="answer-html" html={html} />
        ) : (
          <MessagePrimitive.Content />
        )}
        <ActionBarPrimitive.Root className="message-actions">
          <ActionBarPrimitive.Copy className="icon-button" title="Копировать" aria-label="Копировать">
            <Copy size={14} />
          </ActionBarPrimitive.Copy>
        </ActionBarPrimitive.Root>
      </div>
    </MessagePrimitive.Root>
  );
}

function HtmlBlock({ className, html }: { className?: string; html: string }) {
  const ref = useMathTypeset(html);

  return (
    <div
      className={className}
      dangerouslySetInnerHTML={{ __html: html }}
      ref={ref}
    />
  );
}

function Composer() {
  return (
    <ComposerPrimitive.Root className="composer">
      <ComposerPrimitive.Input
        className="composer-input"
        placeholder="Например: 2.1.4 Учет оперативных ценопринимающих заявок"
        submitMode="enter"
      />
      <ComposerPrimitive.Send className="send-button" title="Отправить" aria-label="Отправить">
        <Send size={18} />
      </ComposerPrimitive.Send>
    </ComposerPrimitive.Root>
  );
}

function EvidencePanel({
  activeTab,
  debugError,
  debugResult,
  isDebugging,
  lastQuestion,
  result,
  onDebug,
  onTabChange
}: {
  activeTab: PanelTab;
  debugError: string;
  debugResult: DebugResponse | null;
  isDebugging: boolean;
  lastQuestion: string;
  result: AskResponse | null;
  onDebug: () => void;
  onTabChange: (tab: PanelTab) => void;
}) {
  return (
    <aside className="evidence-panel">
      <div className="panel-heading">
        <PanelRight size={17} />
        <span>Панели</span>
      </div>

      <div className="panel-tabs" role="tablist" aria-label="Evidence panels">
        <button
          className={activeTab === "evidence" ? "active" : ""}
          onClick={() => onTabChange("evidence")}
          type="button"
        >
          <Sigma size={14} />
          Evidence
        </button>
        <button
          className={activeTab === "sources" ? "active" : ""}
          onClick={() => onTabChange("sources")}
          type="button"
        >
          <Files size={14} />
          Sources
        </button>
        <button
          className={activeTab === "debug" ? "active" : ""}
          onClick={() => onTabChange("debug")}
          type="button"
        >
          <Bug size={14} />
          Debug
        </button>
      </div>

      {!result ? (
        <div className="panel-empty">Здесь появятся источники последнего ответа.</div>
      ) : activeTab === "evidence" ? (
        <EvidenceSummary result={result} />
      ) : activeTab === "sources" ? (
        <SourcesPanel result={result} />
      ) : (
        <DebugPanel
          debugError={debugError}
          debugResult={debugResult}
          fallbackContext={result.context}
          isDebugging={isDebugging}
          lastQuestion={lastQuestion}
          onDebug={onDebug}
        />
      )}
    </aside>
  );
}

function EvidenceSummary({ result }: { result: AskResponse }) {
  return (
    <>
      <div className="metrics">
        <span>{result.from_cache ? "cache" : `${result.elapsed_s.toFixed(1)}с`}</span>
        <span>total ~{result.usage?.total_tokens_est ?? 0}</span>
        <span>answer ~{result.usage?.answer_tokens_est ?? 0}</span>
        <span>context ~{result.usage?.context_tokens_est ?? 0}</span>
      </div>

      <PanelSection title="Расчетные правила">
        {result.formulas?.length ? (
          result.formulas.slice(0, 8).map((formula, index) => (
            <HtmlCard
              className="rule"
              fallback={formula.text}
              html={formula.html}
              key={`${formula.text}-${index}`}
            />
          ))
        ) : (
          <div className="muted">Не выделены.</div>
        )}
      </PanelSection>

      <PanelSection title="Проверка">
        {result.warnings?.length ? (
          result.warnings.map((warning) => (
            <div className="warning" key={warning}>
              <AlertTriangle size={14} />
              <span>{warning}</span>
            </div>
          ))
        ) : (
          <div className="muted">Критичных предупреждений нет.</div>
        )}
      </PanelSection>
    </>
  );
}

function SourcesPanel({ result }: { result: AskResponse }) {
  return (
    <PanelSection title="Источники ответа">
      {result.sources?.length ? (
        result.sources.map((source, index) => (
          <div className="source-item" key={`${source.file}-${index}`}>
            <div className="source-file">
              <FileText size={14} />
              <span>{source.file}</span>
            </div>
            <div className="source-meta">
              {source.section ? <span>{source.section}</span> : null}
              <span>score={source.score}</span>
            </div>
            <HtmlCard
              className="source-preview"
              fallback={source.text_preview}
              html={source.html_preview}
            />
          </div>
        ))
      ) : (
        <div className="muted">Нет источников.</div>
      )}
    </PanelSection>
  );
}

function DebugPanel({
  debugError,
  debugResult,
  fallbackContext,
  isDebugging,
  lastQuestion,
  onDebug
}: {
  debugError: string;
  debugResult: DebugResponse | null;
  fallbackContext?: ApiContextChunk[];
  isDebugging: boolean;
  lastQuestion: string;
  onDebug: () => void;
}) {
  const context = debugResult?.context ?? fallbackContext ?? [];

  return (
    <>
      <button
        className="debug-button"
        disabled={!lastQuestion || isDebugging}
        onClick={onDebug}
        type="button"
      >
        <Bug size={15} />
        {isDebugging ? "Debug..." : "Debug context"}
      </button>

      {debugError ? <div className="warning">{debugError}</div> : null}

      <PanelSection title="Найденный контекст">
        {context.length ? (
          context.map((chunk, index) => (
            <div className="context-item" key={`${chunk.file}-${index}`}>
              <div className="source-file">
                <FileText size={14} />
                <span>{index + 1}. {chunk.file}</span>
              </div>
              <div className="source-meta">
                {chunk.flow_origin ? <span>retrieval: {chunk.flow_origin}</span> : null}
                {chunk.section_path ? <span>{chunk.section_path}</span> : null}
                {chunk.chunk_type ? <span>type: {chunk.chunk_type}</span> : null}
                {chunk.status && chunk.status !== "active" ? <span>status: {chunk.status}</span> : null}
                <span>score={chunk.score}</span>
              </div>
              <HtmlCard
                className="context-text"
                fallback={chunk.text}
                html={chunk.html_text}
              />
            </div>
          ))
        ) : (
          <div className="muted">Нажми Debug context, чтобы получить фрагменты без вызова модели.</div>
        )}
      </PanelSection>
    </>
  );
}

function HtmlCard({
  className,
  fallback,
  html
}: {
  className: string;
  fallback?: string;
  html?: string;
}) {
  if (html) return <HtmlBlock className={className} html={html} />;
  return <div className={className}>{fallback || "Нет данных."}</div>;
}

function PanelSection({
  title,
  children
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="panel-section">
      <div className="panel-section-title">{title}</div>
      {children}
    </section>
  );
}
