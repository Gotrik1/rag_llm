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
  Folder,
  FolderInput,
  PanelRight,
  PanelLeftClose,
  PanelLeftOpen,
  Paintbrush,
  Pencil,
  Pin,
  Plus,
  RefreshCw,
  Save,
  Send,
  Settings2,
  Sigma,
  Sparkles,
  TestTube2,
  Trash2,
  UploadCloud,
  User,
  X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { deleteJson, getJson, patchJson, postFormData, postJson } from "./api";

type Role = "user" | "assistant";

type ChatEntry = {
  id: string;
  role: Role;
  text: string;
  html?: string;
  modelLabel?: string;
  status?: "running" | "complete" | "error";
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

type PanelTab = "system" | "evidence" | "sources" | "debug";
type AppTheme = "default" | "portal";

type WorkspaceState = {
  active_project_id?: string | null;
  active_chat_id?: string | null;
  settings?: Record<string, unknown>;
};

type ProjectRecord = {
  id: string;
  name: string;
  memory?: Record<string, unknown>;
  chats?: ChatRecord[];
};

type ChatRecord = {
  id: string;
  project_id: string | null;
  title: string;
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

type Project = {
  id: string;
  name: string;
  memory: string;
};

type ChatSummary = {
  id: string;
  projectId: string | null;
  title: string;
  messages: ChatEntry[];
};

const DEFAULT_PROJECT: Project = {
  id: "regulations",
  name: "Регламенты",
  memory: "Точный поиск по пунктам регламентов. Формулы и источники показывать явно."
};

const FREE_CHAT_WORKSPACE: Project = {
  id: "",
  name: "Свободный чат",
  memory: "Контекст ограничен текущим чатом."
};

const THEME_KEY = "rag-assistant-theme";
const SIDEBAR_COLLAPSED_KEY = "rag-assistant-sidebar-collapsed";

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

function projectFromRecord(record: ProjectRecord): Project {
  const memory = typeof record.memory?.text === "string" ? record.memory.text : "";
  return { id: record.id, name: record.name, memory };
}

function chatFromRecord(record: ChatRecord): ChatSummary {
  return { id: record.id, projectId: record.project_id, title: record.title, messages: [] };
}

function messageFromRecord(record: MessageRecord): ChatEntry {
  return {
    id: record.id,
    role: record.role,
    text: record.content,
    html: record.content_html || undefined,
    modelLabel: typeof record.metadata?.model_label === "string" ? record.metadata.model_label : undefined,
    status: record.role === "assistant"
      ? record.status === "running" ? "running" : record.status === "error" ? "error" : "complete"
      : "complete"
  };
}

function readStored<T>(key: string, fallback: T): T {
  try {
    const value = window.localStorage.getItem(key);
    return value ? (JSON.parse(value) as T) : fallback;
  } catch {
    return fallback;
  }
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
  const [activeTab, setActiveTab] = useState<PanelTab>("system");
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeProjectId, setActiveProjectId] = useState("");
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [activeChatId, setActiveChatId] = useState("");
  const [workspaceSettings, setWorkspaceSettings] = useState<Record<string, unknown>>({});
  const [workspaceError, setWorkspaceError] = useState("");
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
  const [theme, setTheme] = useState<AppTheme>(() => window.localStorage.getItem(THEME_KEY) === "portal" ? "portal" : "default");
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(() => readStored(SIDEBAR_COLLAPSED_KEY, false));

  const activeProject = activeProjectId
    ? projects.find((project) => project.id === activeProjectId) ?? DEFAULT_PROJECT
    : FREE_CHAT_WORKSPACE;

  useEffect(() => {
    window.localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  useEffect(() => {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, JSON.stringify(isSidebarCollapsed));
  }, [isSidebarCollapsed]);

  const resetThreadState = useCallback(() => {
    setMessages([]);
    setLastResult(null);
    setLastQuestion("");
    setDebugResult(null);
    setDebugError("");
    setActiveTab("system");
  }, []);

  const loadMessages = useCallback(async (chatId: string) => {
    const data = await getJson<{ items: MessageRecord[] }>(`/api/chats/${chatId}/messages`, API_TIMEOUT_MS);
    setMessages(data.items.map(messageFromRecord));
  }, []);

  const syncWorkspace = useCallback(async (projectId: string | null, chatId: string) => {
    const workspace = await postJson<WorkspaceState>("/api/settings", {
      active_project_id: projectId,
      active_chat_id: chatId,
      settings: workspaceSettings
    }, API_TIMEOUT_MS);
    setWorkspaceSettings(workspace.settings ?? workspaceSettings);
  }, [workspaceSettings]);

  const loadWorkspace = useCallback(async () => {
    setWorkspaceError("");
    try {
      const [workspace, projectData, chatData] = await Promise.all([
        getJson<WorkspaceState>("/api/workspace", API_TIMEOUT_MS),
        getJson<{ items: ProjectRecord[] }>("/api/projects", API_TIMEOUT_MS),
        getJson<{ items: ChatRecord[] }>("/api/chats", API_TIMEOUT_MS)
      ]);
      const nextProjects = projectData.items.map(projectFromRecord);
      const nextChats = chatData.items.map(chatFromRecord);
      const nextProjectId = workspace.active_project_id || nextProjects[0]?.id || "";
      const nextChatId = workspace.active_chat_id || nextChats.find((chat) => chat.projectId === null)?.id || nextChats[0]?.id || "";

      setWorkspaceSettings(workspace.settings ?? {});
      setProjects(nextProjects);
      setChats(nextChats);
      setActiveProjectId(nextProjectId);
      setActiveChatId(nextChatId);
      if (nextChatId) await loadMessages(nextChatId);
      else setMessages([]);
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось загрузить проекты и чаты");
    }
  }, [loadMessages]);

  const loadModels = useCallback(async () => {
    setIsLoadingModels(true);
    setModelError("");
    try {
      const data = await getJson<ModelsResponse>("/api/models", API_TIMEOUT_MS);
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

  const createChat = useCallback(async (projectId: string) => {
    const record = await postJson<ChatRecord>(`/api/projects/${projectId}/chats`, { title: "Новый чат" }, API_TIMEOUT_MS);
    const chat = chatFromRecord(record);
    setChats((current) => [...current, chat]);
    setActiveProjectId(projectId);
    setActiveChatId(chat.id);
    await syncWorkspace(projectId, chat.id);
    resetThreadState();
    return chat;
  }, [resetThreadState, syncWorkspace]);

  const createFreeChat = useCallback(async () => {
    const record = await postJson<ChatRecord>("/api/chats", { title: "Новый чат" }, API_TIMEOUT_MS);
    const chat = chatFromRecord(record);
    setChats((current) => [...current, chat]);
    setActiveProjectId("");
    setActiveChatId(chat.id);
    await syncWorkspace(null, chat.id);
    resetThreadState();
    return chat;
  }, [resetThreadState, syncWorkspace]);

  const openChat = useCallback(async (chat: ChatSummary) => {
    setActiveProjectId(chat.projectId ?? "");
    setActiveChatId(chat.id);
    await syncWorkspace(chat.projectId, chat.id);
    await loadMessages(chat.id);
    setLastResult(null);
    setLastQuestion("");
    setDebugResult(null);
    setDebugError("");
    setActiveTab("system");
  }, [loadMessages, syncWorkspace]);

  const selectProject = useCallback(async (projectId: string) => {
    const chat = chats.find((item) => item.projectId === projectId);
    if (chat) {
      await openChat(chat);
      return;
    }
    await createChat(projectId);
  }, [chats, createChat, openChat]);

  const createProject = useCallback(async () => {
    const name = window.prompt("Название проекта", "Новый проект")?.trim();
    if (!name) return;
    try {
      const record = await postJson<ProjectRecord>("/api/projects", { name, memory: { text: "Память проекта пока пуста." } }, API_TIMEOUT_MS);
      setProjects((current) => [...current, projectFromRecord(record)]);
      await createChat(record.id);
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось создать проект");
    }
  }, [createChat]);

  const updateProjectMemory = useCallback(async (memory: string) => {
    if (!activeProjectId) return;
    const previous = projects;
    setProjects((current) => current.map((project) => project.id === activeProjectId ? { ...project, memory } : project));
    try {
      await patchJson<ProjectRecord>(`/api/projects/${activeProjectId}`, { memory: { text: memory } }, API_TIMEOUT_MS);
    } catch (error) {
      setProjects(previous);
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось сохранить память проекта");
    }
  }, [activeProjectId, projects]);

  const renameProject = useCallback(async (projectId: string, name: string) => {
    const project = projects.find((item) => item.id === projectId);
    if (!project) return;
    if (!name || name === project.name) return;
    try {
      const record = await patchJson<ProjectRecord>(`/api/projects/${projectId}`, { name }, API_TIMEOUT_MS);
      setProjects((current) => current.map((item) => item.id === projectId ? projectFromRecord(record) : item));
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось переименовать проект");
    }
  }, [projects]);

  const renameChat = useCallback(async (chatId: string, title: string) => {
    const chat = chats.find((item) => item.id === chatId);
    if (!chat) return;
    if (!title || title === chat.title) return;
    try {
      const record = await patchJson<ChatRecord>(`/api/chats/${chatId}`, { title }, API_TIMEOUT_MS);
      setChats((current) => current.map((item) => item.id === chatId ? chatFromRecord(record) : item));
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось переименовать чат");
    }
  }, [chats]);

  const deleteChat = useCallback(async (chatId: string) => {
    const chat = chats.find((item) => item.id === chatId);
    if (!chat || !window.confirm(`Удалить чат «${chat.title}»?`)) return;
    try {
      await deleteJson<{ deleted: boolean }>(`/api/chats/${chatId}`, API_TIMEOUT_MS);
      setChats((current) => current.filter((item) => item.id !== chatId));
      if (activeChatId === chatId) {
        setActiveChatId("");
        resetThreadState();
        await syncWorkspace(null, "");
      }
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось удалить чат");
    }
  }, [activeChatId, chats, resetThreadState, syncWorkspace]);

  const deleteProject = useCallback(async (projectId: string) => {
    const project = projects.find((item) => item.id === projectId);
    if (!project || !window.confirm(`Удалить проект «${project.name}»? Его чаты станут свободными.`)) return;
    try {
      await deleteJson<{ deleted: boolean }>(`/api/projects/${projectId}`, API_TIMEOUT_MS);
      setProjects((current) => current.filter((item) => item.id !== projectId));
      setChats((current) => current.map((item) => item.projectId === projectId ? { ...item, projectId: null } : item));
      if (activeProjectId === projectId) {
        setActiveProjectId("");
        await syncWorkspace(null, activeChatId);
      }
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось удалить проект");
    }
  }, [activeChatId, activeProjectId, projects, syncWorkspace]);

  const moveChat = useCallback(async (chatId: string, projectId: string): Promise<boolean> => {
    const chat = chats.find((item) => item.id === chatId);
    if (!chat) return false;
    try {
      const record = await patchJson<ChatRecord>(`/api/chats/${chatId}`, { project_id: projectId }, API_TIMEOUT_MS);
      setChats((current) => current.map((item) => item.id === chatId ? chatFromRecord(record) : item));
      if (activeChatId === chatId) {
        setActiveProjectId(projectId);
        await syncWorkspace(projectId, chatId);
      }
      return true;
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось перенести чат в проект");
      return false;
    }
  }, [activeChatId, chats, syncWorkspace]);

  const updatePinnedItems = useCallback(async (kind: "project" | "chat", itemId: string) => {
    const settingKey = kind === "project" ? "pinned_project_ids" : "pinned_chat_ids";
    const current = Array.isArray(workspaceSettings[settingKey])
      ? workspaceSettings[settingKey].filter((value): value is string => typeof value === "string")
      : [];
    const nextIds = current.includes(itemId)
      ? current.filter((id) => id !== itemId)
      : [...current, itemId];
    const nextSettings = { ...workspaceSettings, [settingKey]: nextIds };
    try {
      const workspace = await postJson<WorkspaceState>("/api/settings", {
        active_project_id: activeProjectId || null,
        active_chat_id: activeChatId || null,
        settings: nextSettings
      }, API_TIMEOUT_MS);
      setWorkspaceSettings(workspace.settings ?? nextSettings);
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось обновить закреплённые элементы");
    }
  }, [activeChatId, activeProjectId, workspaceSettings]);

  const pinnedProjectIds = useMemo(
    () => Array.isArray(workspaceSettings.pinned_project_ids)
      ? workspaceSettings.pinned_project_ids.filter((value): value is string => typeof value === "string")
      : [],
    [workspaceSettings]
  );
  const pinnedChatIds = useMemo(
    () => Array.isArray(workspaceSettings.pinned_chat_ids)
      ? workspaceSettings.pinned_chat_ids.filter((value): value is string => typeof value === "string")
      : [],
    [workspaceSettings]
  );

  const onNew = useCallback(async (message: AppendMessage) => {
    const question = extractText(message);
    if (!question || !activeChatId) return;

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
    setActiveTab("system");

    try {
      await postJson<MessageRecord>(`/api/chats/${activeChatId}/messages`, { role: "user", content: question }, API_TIMEOUT_MS);
      const data = await postJson<AskResponse>("/api/ask", { question, chat_id: activeChatId }, ASK_TIMEOUT_MS);
      await postJson<MessageRecord>(`/api/chats/${activeChatId}/messages`, {
        role: "assistant",
        content: data.answer || "Пустой ответ.",
        content_html: data.html_answer || "",
        status: "complete",
        metadata: { model_label: data.llm?.label, sources: data.sources, usage: data.usage }
      }, API_TIMEOUT_MS);

      const currentChat = chats.find((chat) => chat.id === activeChatId);
      if (currentChat && currentChat.title === "Новый чат") {
        const title = question.slice(0, 48);
        await patchJson<ChatRecord>(`/api/chats/${activeChatId}`, { title }, API_TIMEOUT_MS);
        setChats((current) => current.map((chat) => chat.id === activeChatId ? { ...chat, title } : chat));
      }

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
      void postJson<MessageRecord>(`/api/chats/${activeChatId}/messages`, {
        role: "assistant",
        content: text,
        status: "error"
      }, API_TIMEOUT_MS).catch(() => undefined);
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
  }, [activeChatId, chats]);

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

  const clearThread = useCallback(async () => {
    try {
      await createFreeChat();
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось создать чат");
    }
  }, [createFreeChat]);

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
      <main className={`app-shell theme-${theme} ${isSidebarCollapsed ? "sidebar-collapsed" : ""}`}>
        <aside className="sidebar">
          <div className="sidebar-topbar">
            <div className="sidebar-title">AI ИАС Энергобаланс</div>
            <button
              className="sidebar-collapse"
              onClick={() => setIsSidebarCollapsed((value) => !value)}
              title={isSidebarCollapsed ? "Развернуть левый сайдбар" : "Свернуть левый сайдбар"}
              type="button"
            >
              {isSidebarCollapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
            </button>
          </div>
          <button className="new-thread" onClick={() => void clearThread()} type="button">
            <span className="new-thread-icon"><Plus size={16} /></span>
            <span>Новый диалог</span>
          </button>
          <NavigationSidebar
            activeProjectId={activeProjectId}
            chats={chats}
            pinnedChatIds={pinnedChatIds}
            pinnedProjectIds={pinnedProjectIds}
            projects={projects}
            onChatChange={(chat) => void openChat(chat)}
            onChatMove={moveChat}
            onChatDelete={(chatId) => void deleteChat(chatId)}
            onChatPin={(chatId) => void updatePinnedItems("chat", chatId)}
            onChatRename={(chatId, title) => void renameChat(chatId, title)}
            onProjectChange={(projectId) => void selectProject(projectId)}
            onProjectChatCreate={(projectId) => void createChat(projectId)}
            onProjectCreate={() => void createProject()}
            onProjectDelete={(projectId) => void deleteProject(projectId)}
            onProjectPin={(projectId) => void updatePinnedItems("project", projectId)}
            onProjectRename={(projectId, name) => void renameProject(projectId, name)}
          />
          {workspaceError && <div className="sidebar-note">{workspaceError}</div>}
        </aside>

        <section className="chat-surface">
          <Thread />
        </section>

        <EvidencePanel
          activeTab={activeTab}
          activeProject={activeProject}
          activeProvider={activeProvider}
          theme={theme}
          debugError={debugError}
          debugResult={debugResult}
          isDebugging={isDebugging}
          isChangingModel={isChangingModel}
          isLoadingModels={isLoadingModels}
          isRunning={isRunning}
          lastQuestion={lastQuestion}
          modelError={modelError}
          models={models}
          result={lastResult}
          selectedModel={selectedModel}
          onChangeModel={changeModel}
          onDebug={runDebug}
          error={uploadError}
          isUploading={isUploading}
          onProviderSettingsChange={setActiveProvider}
          onProjectMemoryChange={(memory) => void updateProjectMemory(memory)}
          onRefreshModels={loadModels}
          onTabChange={setActiveTab}
          onThemeChange={setTheme}
          selectedFiles={selectedFiles}
          uploadResult={uploadResult}
          onFilesChange={setSelectedFiles}
          onUpload={uploadFiles}
        />
      </main>
    </AssistantRuntimeProvider>
  );
}

function NavigationSidebar({
  activeProjectId,
  chats,
  pinnedChatIds,
  pinnedProjectIds,
  projects,
  onChatChange,
  onChatDelete,
  onChatMove,
  onChatPin,
  onChatRename,
  onProjectChange,
  onProjectChatCreate,
  onProjectCreate,
  onProjectDelete,
  onProjectPin,
  onProjectRename
}: {
  activeProjectId: string;
  chats: ChatSummary[];
  pinnedChatIds: string[];
  pinnedProjectIds: string[];
  projects: Project[];
  onChatChange: (chat: ChatSummary) => void;
  onChatDelete: (chatId: string) => void;
  onChatMove: (chatId: string, projectId: string) => Promise<boolean>;
  onChatPin: (chatId: string) => void;
  onChatRename: (chatId: string, title: string) => void;
  onProjectChange: (projectId: string) => void;
  onProjectChatCreate: (projectId: string) => void;
  onProjectCreate: () => void;
  onProjectDelete: (projectId: string) => void;
  onProjectPin: (projectId: string) => void;
  onProjectRename: (projectId: string, name: string) => void;
}) {
  const [showAllProjects, setShowAllProjects] = useState(false);
  const [movingChatId, setMovingChatId] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ kind: "project" | "chat"; id: string; value: string } | null>(null);
  const visibleProjects = showAllProjects ? projects : projects.slice(0, 5);
  const pinnedProjects = projects.filter((project) => pinnedProjectIds.includes(project.id));
  const pinnedChats = chats.filter((chat) => pinnedChatIds.includes(chat.id));
  const freeChats = chats.filter((chat) => chat.projectId === null);
  const projectNames = new Map(projects.map((project) => [project.id, project.name]));

  const saveEdit = () => {
    if (!editing) return;
    const value = editing.value.trim();
    if (!value) return;
    if (editing.kind === "project") onProjectRename(editing.id, value);
    else onChatRename(editing.id, value);
    setEditing(null);
  };

  const editProps = (kind: "project" | "chat", id: string, value: string) => ({
    editing: editing?.kind === kind && editing.id === id,
    editValue: editing?.kind === kind && editing.id === id ? editing.value : value,
    onEditChange: (next: string) => setEditing((current) => current ? { ...current, value: next } : current),
    onEditCancel: () => setEditing(null),
    onEditSave: saveEdit,
    onRename: () => setEditing({ kind, id, value })
  });

  const renderChat = (chat: ChatSummary, nested = false) => (
    <div className={`nav-chat-group ${nested ? "project-chat" : ""}`} key={chat.id}>
      <NavigationItem
        chat
        label={chat.title}
        onDelete={() => onChatDelete(chat.id)}
        onMove={() => setMovingChatId((current) => current === chat.id ? null : chat.id)}
        onOpen={() => onChatChange(chat)}
        onPin={() => onChatPin(chat.id)}
        pinned={pinnedChatIds.includes(chat.id)}
        {...editProps("chat", chat.id, chat.title)}
      />
      {movingChatId === chat.id ? (
        <div className="nav-move-menu">
          <div className="nav-move-title">Перенести в проект</div>
          {projects.map((project) => (
            <button
              className={project.id === chat.projectId ? "nav-move-current" : undefined}
              key={project.id}
              onClick={() => void onChatMove(chat.id, project.id).then((moved) => {
                if (moved) setMovingChatId(null);
              })}
              type="button"
            >
              <Folder size={14} />
              <span>{project.name}</span>
              {project.id === chat.projectId ? <small>Текущий проект</small> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );

  return (
    <nav className="navigation-sidebar" aria-label="Проекты и чаты">
      <section className="nav-section">
        <div className="nav-section-title">Закреплённые</div>
        <div className="project-list pinned-list">
          {pinnedProjects.map((project) => (
            <NavigationItem
              active={project.id === activeProjectId}
              icon={<Folder size={17} />}
              key={project.id}
              label={project.name}
              onCreateChat={() => onProjectChatCreate(project.id)}
              onOpen={() => onProjectChange(project.id)}
              onDelete={() => onProjectDelete(project.id)}
              onPin={() => onProjectPin(project.id)}
              onRename={() => setEditing({ kind: "project", id: project.id, value: project.name })}
              pinned
            />
          ))}
          {pinnedChats.map((chat) => (
            <NavigationItem
              chat
              detail={chat.projectId ? projectNames.get(chat.projectId) : undefined}
              key={chat.id}
              label={chat.title}
              onDelete={() => onChatDelete(chat.id)}
              onMove={() => setMovingChatId((current) => current === chat.id ? null : chat.id)}
              onOpen={() => onChatChange(chat)}
              onPin={() => onChatPin(chat.id)}
              onRename={() => setEditing({ kind: "chat", id: chat.id, value: chat.title })}
              pinned
            />
          ))}
        </div>
      </section>

      <section className="nav-section">
        <div className="nav-section-header">
          <div className="nav-section-title">Проекты</div>
          <button className="nav-add-project" onClick={onProjectCreate} title="Новый проект" type="button"><Plus size={15} /></button>
        </div>
        <div className="project-list">
          {visibleProjects.map((project) => (
            <div className="project-tree" key={project.id}>
              <NavigationItem
                active={project.id === activeProjectId}
                icon={<Folder size={17} />}
                label={project.name}
                onCreateChat={() => onProjectChatCreate(project.id)}
                onDelete={() => onProjectDelete(project.id)}
                onOpen={() => onProjectChange(project.id)}
                onPin={() => onProjectPin(project.id)}
                pinned={pinnedProjectIds.includes(project.id)}
                {...editProps("project", project.id, project.name)}
              />
              <div className="project-chat-list">
                {chats.filter((chat) => chat.projectId === project.id).map((chat) => renderChat(chat, true))}
              </div>
            </div>
          ))}
        </div>
        {projects.length > 5 ? (
          <button className="show-more" onClick={() => setShowAllProjects((value) => !value)} type="button">
            {showAllProjects ? "Скрыть" : "Показать еще"}
          </button>
        ) : null}
      </section>

      <section className="nav-section chats-section">
        <div className="nav-section-title">Чаты</div>
        <div className="sidebar-chat-list">
          {freeChats.length ? freeChats.map((chat) => renderChat(chat)) : <div className="sidebar-empty">Свободных чатов пока нет.</div>}
        </div>
      </section>
    </nav>
  );
}

function NavigationItem({
  active = false,
  chat = false,
  detail,
  icon,
  label,
  onCreateChat,
  onDelete,
  onEditCancel,
  onEditChange,
  onEditSave,
  onMove,
  onOpen,
  onPin,
  onRename,
  pinned = false,
  editing = false,
  editValue = ""
}: {
  active?: boolean;
  chat?: boolean;
  detail?: string;
  icon?: ReactNode;
  label: string;
  onCreateChat?: () => void;
  onDelete?: () => void;
  onEditCancel?: () => void;
  onEditChange?: (value: string) => void;
  onEditSave?: () => void;
  onMove?: () => void;
  onOpen: () => void;
  onPin?: () => void;
  onRename?: () => void;
  pinned?: boolean;
  editing?: boolean;
  editValue?: string;
}) {
  const [menuAnchor, setMenuAnchor] = useState<{ top: number; left: number } | null>(null);
  const closeMenuTimer = useRef<number | null>(null);
  const hasActionMenu = Boolean(onCreateChat || onMove || onPin || onRename || onDelete);

  const clearMenuCloseTimer = () => {
    if (closeMenuTimer.current !== null) {
      window.clearTimeout(closeMenuTimer.current);
      closeMenuTimer.current = null;
    }
  };

  const openActionMenu = (element: HTMLDivElement) => {
    clearMenuCloseTimer();
    const rect = element.getBoundingClientRect();
    setMenuAnchor({ top: rect.top, left: rect.right + 8 });
  };

  const scheduleActionMenuClose = () => {
    clearMenuCloseTimer();
    closeMenuTimer.current = window.setTimeout(() => setMenuAnchor(null), 140);
  };

  useEffect(() => () => clearMenuCloseTimer(), []);

  const actionMenu = !editing && hasActionMenu && menuAnchor && typeof document !== "undefined"
    ? createPortal(
      <div
        className="nav-hover-menu"
        onMouseEnter={clearMenuCloseTimer}
        onMouseLeave={scheduleActionMenuClose}
        role="menu"
        style={{ left: menuAnchor.left, top: menuAnchor.top }}
      >
        <button className="nav-menu-action" onClick={onOpen} type="button"><Settings2 size={14} /><span>Редактировать</span></button>
        {onCreateChat ? <button className="nav-menu-action" onClick={onCreateChat} type="button"><Plus size={14} /><span>Новый чат</span></button> : null}
        {onMove ? <button className="nav-menu-action" onClick={onMove} type="button"><FolderInput size={14} /><span>Перенести в проект</span></button> : null}
        {onPin ? <button className="nav-menu-action" onClick={onPin} type="button"><Pin size={14} /><span>{pinned ? "Открепить" : "Закрепить"}</span></button> : null}
        {onRename ? <button className="nav-menu-action" onClick={onRename} type="button"><Pencil size={14} /><span>Переименовать</span></button> : null}
        {onDelete ? <button className="nav-menu-action danger" onClick={onDelete} type="button"><Trash2 size={14} /><span>Удалить</span></button> : null}
      </div>,
      document.body
    )
    : null;

  return (
    <div
      className="nav-row"
      onMouseEnter={(event) => hasActionMenu && openActionMenu(event.currentTarget)}
      onMouseLeave={scheduleActionMenuClose}
    >
      {editing ? (
        <div className={`nav-item nav-item-edit ${chat ? "chat-nav-item" : ""} ${active ? "active" : ""}`}>
          {icon}
          <input
            aria-label="Новое название"
            autoFocus
            onChange={(event) => onEditChange?.(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") onEditSave?.();
              if (event.key === "Escape") onEditCancel?.();
            }}
            value={editValue}
          />
          <button className="nav-inline-action" onClick={onEditSave} title="Сохранить" type="button"><CheckCircle2 size={14} /></button>
          <button className="nav-inline-action" onClick={onEditCancel} title="Отменить" type="button"><X size={14} /></button>
        </div>
      ) : (
        <button className={`nav-item ${chat ? "chat-nav-item" : ""} ${active ? "active" : ""}`} onClick={onOpen} type="button">
          {icon}
          {detail ? <span className="nav-item-copy"><span>{label}</span><small>{detail}</small></span> : <span>{label}</span>}
        </button>
      )}
      {actionMenu}
    </div>
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

      </ThreadPrimitive.Viewport>
      <ThreadPrimitive.ViewportFooter className="thread-footer">
        <Composer />
      </ThreadPrimitive.ViewportFooter>
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
  activeProject,
  activeProvider,
  theme,
  debugError,
  debugResult,
  isDebugging,
  isChangingModel,
  isLoadingModels,
  isRunning,
  lastQuestion,
  modelError,
  models,
  result,
  selectedModel,
  onChangeModel,
  onDebug,
  onProviderSettingsChange,
  onProjectMemoryChange,
  onRefreshModels,
  onTabChange,
  onThemeChange,
  error,
  isUploading,
  selectedFiles,
  uploadResult,
  onFilesChange,
  onUpload
}: {
  activeTab: PanelTab;
  activeProject: Project;
  activeProvider: ProviderName;
  theme: AppTheme;
  debugError: string;
  debugResult: DebugResponse | null;
  isDebugging: boolean;
  isChangingModel: boolean;
  isLoadingModels: boolean;
  isRunning: boolean;
  lastQuestion: string;
  modelError: string;
  models: string[];
  result: AskResponse | null;
  selectedModel: string;
  onChangeModel: (model: string) => void;
  onDebug: () => void;
  onProviderSettingsChange: (provider: ProviderName) => void;
  onProjectMemoryChange: (memory: string) => void;
  onRefreshModels: () => void;
  onTabChange: (tab: PanelTab) => void;
  onThemeChange: (theme: AppTheme) => void;
  error: string;
  isUploading: boolean;
  selectedFiles: File[];
  uploadResult: UploadResponse | null;
  onFilesChange: (files: File[]) => void;
  onUpload: () => void;
}) {
  const [isThemeModalOpen, setIsThemeModalOpen] = useState(false);

  return (
    <aside className="evidence-panel">
      <div className="workspace-heading">
        <div className="panel-heading">
          <PanelRight size={17} />
          <span>Техническая панель</span>
        </div>
        <button className="icon-button panel-settings" onClick={() => setIsThemeModalOpen(true)} title="Настройки рабочей области" type="button">
          <Settings2 size={16} />
        </button>
      </div>

      <div className="panel-tabs" role="tablist" aria-label="Evidence panels">
        <button
          className={activeTab === "system" ? "active" : ""}
          onClick={() => onTabChange("system")}
          type="button"
        >
          <Settings2 size={14} />
          Система
        </button>
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

      {activeTab === "system" ? (
        <div className="system-panel">
          <div className="system-intro">
            <div className="system-kicker">Рабочие настройки</div>
            <h2>Система</h2>
            <p>Параметры ниже применяются к следующим запросам.</p>
          </div>
          <section className="project-memory-panel">
            <div className="project-memory-heading"><Folder size={14} /><span>{activeProject.name}</span></div>
            <label>
              <span>Память проекта</span>
              <textarea value={activeProject.memory} onChange={(event) => onProjectMemoryChange(event.target.value)} />
            </label>
          </section>
          <ProviderSettings models={models} onProviderChange={onProviderSettingsChange} />
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
              onChange={onChangeModel}
              onRefresh={onRefreshModels}
            />
          ) : null}
          <KnowledgeLoader
            error={error}
            isUploading={isUploading}
            result={uploadResult}
            selectedFiles={selectedFiles}
            onFilesChange={onFilesChange}
            onUpload={onUpload}
          />
        </div>
      ) : !result ? (
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

      {isThemeModalOpen ? (
        <div className="theme-modal-backdrop" role="presentation" onMouseDown={() => setIsThemeModalOpen(false)}>
          <section className="theme-modal" role="dialog" aria-modal="true" aria-labelledby="theme-modal-title" onMouseDown={(event) => event.stopPropagation()}>
            <div className="theme-modal-heading">
              <div>
                <div className="system-kicker">Настройки интерфейса</div>
                <h2 id="theme-modal-title">Тема</h2>
              </div>
              <button className="icon-button" onClick={() => setIsThemeModalOpen(false)} title="Закрыть" type="button"><X size={17} /></button>
            </div>
            <ThemeSelector theme={theme} onChange={onThemeChange} />
          </section>
        </div>
      ) : null}
    </aside>
  );
}

function ThemeSelector({ theme, onChange }: { theme: AppTheme; onChange: (theme: AppTheme) => void }) {
  return (
    <section className="theme-selector" aria-label="Тема интерфейса">
      <div className="theme-selector-title"><Paintbrush size={14} /> Тема интерфейса</div>
      <select value={theme} onChange={(event) => onChange(event.target.value as AppTheme)}>
        <option value="default">Базовая</option>
        <option value="portal">Корпоративная</option>
      </select>
      <div className="theme-selector-description">
        {theme === "portal" ? "Контрастная палитра для встраивания в корпоративный портал." : "Нейтральная тема RAG Assistant."}
      </div>
    </section>
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
