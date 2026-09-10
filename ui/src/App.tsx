import {
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
import ReactMarkdown from "react-markdown";
import "katex/dist/katex.min.css";
import rehypeKatex from "rehype-katex";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
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
  Image as ImageIcon,
  LockKeyhole,
  LogOut,
  Maximize2,
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
  X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { clearCsrfToken, deleteJson, getJson, patchJson, postFormData, postJson, setCsrfToken } from "./api";

type Role = "user" | "assistant";
type UploadTarget = "global" | "project" | "chat";

type ChatEntry = {
  id: string;
  role: Role;
  text: string;
  html?: string;
  modelLabel?: string;
  sources?: ApiSource[];
  status?: "running" | "complete" | "error";
};

type ApiSource = {
  file: string;
  url?: string;
  section?: string;
  locator?: string;
  page_number?: number;
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
  message_id?: string;
  job_id?: string;
  status?: "running" | "complete";
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
  queued?: boolean;
  files: {
    id: string;
    file: string;
    job_id: string;
    status: string;
    url: string;
    size_bytes: number;
  }[];
};

type DocumentRecord = {
  id: string;
  file_name: string;
  status: "queued" | "processing" | "ready" | "failed" | "cancelled";
  processing_state?: { job_id?: string; progress?: number; error?: string };
  metadata?: { chunks?: number; size_bytes?: number };
  created_at?: string;
  document_family?: string;
  version_label?: string | null;
  version_source?: "metadata" | "file_name" | null;
  version_status?: "latest" | "superseded" | "unknown";
  newer_document_id?: string | null;
  newer_file_name?: string | null;
  scope: "global" | "project";
  project_id?: string | null;
  chat_id?: string | null;
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
  knowledge_scope?: KnowledgeScope;
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
  knowledgeScope: KnowledgeScope;
};

type KnowledgeScope = "project_only" | "project_and_global";

type AuthUser = {
  id: string;
  email: string;
  display_name: string;
  is_admin: boolean;
  can_manage_global_documents: boolean;
};

type SessionResponse = {
  authenticated: boolean;
  user: AuthUser | null;
  csrf_token: string | null;
};

type ChatSummary = {
  id: string;
  projectId: string | null;
  title: string;
  messages: ChatEntry[];
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
  return {
    id: record.id,
    name: record.name,
    memory,
    knowledgeScope: record.knowledge_scope === "project_only" ? "project_only" : "project_and_global"
  };
}

function chatFromRecord(record: ChatRecord): ChatSummary {
  return { id: record.id, projectId: record.project_id, title: record.title, messages: [] };
}

function messageFromRecord(record: MessageRecord): ChatEntry {
  const sources = Array.isArray(record.metadata?.sources) ? record.metadata.sources as ApiSource[] : [];
  return {
    id: record.id,
    role: record.role,
    text: record.content,
    html: record.content_html || undefined,
    modelLabel: typeof record.metadata?.model_label === "string" ? record.metadata.model_label : undefined,
    sources,
    status: record.role === "assistant"
      ? record.status === "running" ? "running" : record.status === "error" ? "error" : "complete"
      : "complete"
  };
}

export function resultFromMessageRecord(record: MessageRecord): AskResponse | null {
  if (record.role !== "assistant" || record.status === "running") return null;
  const metadata = record.metadata ?? {};
  const sources = Array.isArray(metadata.sources) ? metadata.sources as ApiSource[] : [];
  const formulas = Array.isArray(metadata.formulas) ? metadata.formulas as ApiFormula[] : [];
  const warnings = Array.isArray(metadata.warnings)
    ? metadata.warnings.filter((item): item is string => typeof item === "string")
    : [];
  const usage = metadata.usage && typeof metadata.usage === "object"
    ? metadata.usage as AskResponse["usage"]
    : undefined;
  const llm = metadata.llm && typeof metadata.llm === "object"
    ? metadata.llm as AskResponse["llm"]
    : undefined;
  return {
    answer: record.content,
    message_id: record.id,
    status: record.status === "error" ? undefined : "complete",
    html_answer: record.content_html,
    from_cache: metadata.from_cache === true,
    elapsed_s: typeof metadata.elapsed_s === "number" ? metadata.elapsed_s : 0,
    sources,
    formulas,
    warnings,
    usage,
    llm
  };
}

export function panelStateFromMessageRecords(records: MessageRecord[]): {
  result: AskResponse | null;
  question: string;
} {
  for (let answerIndex = records.length - 1; answerIndex >= 0; answerIndex -= 1) {
    const result = resultFromMessageRecord(records[answerIndex]);
    if (!result) continue;
    for (let questionIndex = answerIndex - 1; questionIndex >= 0; questionIndex -= 1) {
      const record = records[questionIndex];
      if (record.role === "user" && record.content.trim()) {
        return { result, question: record.content };
      }
    }
    return { result, question: "" };
  }
  const latestQuestion = [...records]
    .reverse()
    .find((record) => record.role === "user" && record.content.trim());
  return { result: null, question: latestQuestion?.content ?? "" };
}

export function hasRunningAssistantMessage(records: Array<Pick<MessageRecord, "role" | "status">>): boolean {
  return records.some((record) => record.role === "assistant" && record.status === "running");
}

export function generationResponseIsTerminal(status?: AskResponse["status"]): boolean {
  return status !== "running";
}

function fileCountLabel(count: number): string {
  const mod100 = count % 100;
  const mod10 = count % 10;
  const noun = mod100 >= 11 && mod100 <= 14
    ? "файлов"
    : mod10 === 1
      ? "файл"
      : mod10 >= 2 && mod10 <= 4
        ? "файла"
        : "файлов";
  return `${count} ${noun}`;
}

export function uploadFeedback(
  result: UploadResponse | null,
  documents: DocumentRecord[]
): { state: "pending" | "success" | "error"; message: string } | null {
  if (!result?.files.length) return null;
  const documentsById = new Map(documents.map((document) => [document.id, document]));
  const statuses = result.files.map((file) => documentsById.get(file.id)?.status ?? file.status);
  const ready = statuses.filter((status) => status === "ready").length;
  const failed = statuses.filter((status) => status === "failed" || status === "cancelled").length;
  const total = statuses.length;

  if (failed) {
    const completed = ready ? ` Готово: ${ready}.` : "";
    return {
      state: "error",
      message: `Не обработано: ${failed} из ${total}.${completed} Проверьте статус документа слева.`
    };
  }
  if (ready === total) {
    return {
      state: "success",
      message: `${fileCountLabel(total)} ${total === 1 ? "готов и добавлен" : "готовы и добавлены"} в базу знаний`
    };
  }
  return {
    state: "pending",
    message: ready
      ? `Готово ${ready} из ${total} · остальные обрабатываются`
      : `${fileCountLabel(total)} ${total === 1 ? "обрабатывается" : "обрабатываются"}…`
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
      modelLabel: message.modelLabel,
      sources: message.sources
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

export function splitDocumentsByScope(documents: DocumentRecord[]): {
  global: DocumentRecord[];
  project: DocumentRecord[];
  chat: DocumentRecord[];
} {
  return {
    global: documents.filter((document) => document.scope === "global"),
    project: documents.filter((document) => document.scope !== "global" && !document.chat_id),
    chat: documents.filter((document) => document.scope !== "global" && Boolean(document.chat_id))
  };
}

export function uploadScopeFields(
  target: UploadTarget,
  activeProjectId: string,
  activeChatId: string
): { project_id?: string; chat_id?: string } {
  if (target === "global") return {};
  if (target === "project") {
    if (!activeProjectId) throw new Error("Сначала откройте проект.");
    return { project_id: activeProjectId };
  }
  if (!activeChatId) throw new Error("Сначала откройте чат.");
  return { chat_id: activeChatId };
}

export function App() {
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [sessionError, setSessionError] = useState("");

  const refreshSession = useCallback(async () => {
    setSessionError("");
    try {
      const next = await getJson<SessionResponse>("/api/auth/session", API_TIMEOUT_MS);
      setCsrfToken(next.csrf_token);
      setSession(next);
    } catch (error) {
      clearCsrfToken();
      setSession({ authenticated: false, user: null, csrf_token: null });
      setSessionError(error instanceof Error ? error.message : "Сервис временно недоступен");
    }
  }, []);

  useEffect(() => {
    void refreshSession();
    const handleUnauthorized = () => {
      clearCsrfToken();
      setSession({ authenticated: false, user: null, csrf_token: null });
    };
    window.addEventListener("rag:unauthorized", handleUnauthorized);
    return () => window.removeEventListener("rag:unauthorized", handleUnauthorized);
  }, [refreshSession]);

  const logout = useCallback(async () => {
    try {
      await postJson<{ authenticated: boolean }>("/api/auth/logout", {}, API_TIMEOUT_MS);
    } finally {
      clearCsrfToken();
      setSession({ authenticated: false, user: null, csrf_token: null });
    }
  }, []);

  if (session === null) {
    return <div className="session-loading" role="status">Открываем рабочее пространство…</div>;
  }
  if (!session.authenticated || !session.user) {
    return <AuthPage initialError={sessionError} onAuthenticated={setSession} />;
  }
  return <WorkspaceApp user={session.user} onLogout={logout} />;
}

function AuthPage({ initialError, onAuthenticated }: {
  initialError: string;
  onAuthenticated: (session: SessionResponse) => void;
}) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState(initialError);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;
    if (mode === "register" && password !== confirmation) {
      setError("Пароли не совпадают.");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const path = mode === "login" ? "/api/auth/login" : "/api/auth/register";
      const payload = mode === "login"
        ? { email: email.trim(), password }
        : { email: email.trim(), display_name: displayName.trim(), password };
      const session = await postJson<SessionResponse>(path, payload, API_TIMEOUT_MS);
      setCsrfToken(session.csrf_token);
      onAuthenticated(session);
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "Не удалось выполнить вход");
    } finally {
      setSubmitting(false);
    }
  };

  const changeMode = (next: "login" | "register") => {
    setMode(next);
    setError("");
    setPassword("");
    setConfirmation("");
  };

  return (
    <main className="auth-shell">
      <section className="auth-intro" aria-labelledby="auth-product-title">
        <div className="auth-brand"><Bot size={22} /> AI ИАС Энергобаланс</div>
        <div>
          <div className="auth-eyebrow">Работа с корпоративными знаниями</div>
          <h1 id="auth-product-title">Документы отвечают.<br />Источники остаются проверяемыми.</h1>
          <p>Общая библиотека доступна всем пользователям, а документы, чаты и память проекта остаются в вашем рабочем пространстве.</p>
        </div>
        <div className="auth-principles" aria-label="Возможности">
          <span><FileText size={16} /> Ссылка на документ и точный ориентир</span>
          <span><Sigma size={16} /> Формулы без потери разметки</span>
          <span><LockKeyhole size={16} /> Изолированные проекты и сохранённые сессии</span>
        </div>
      </section>

      <section className="auth-form-panel" aria-labelledby="auth-form-title">
        <div className="auth-mode" role="tablist" aria-label="Вход или регистрация">
          <button className={mode === "login" ? "active" : ""} onClick={() => changeMode("login")} role="tab" type="button">Вход</button>
          <button className={mode === "register" ? "active" : ""} onClick={() => changeMode("register")} role="tab" type="button">Регистрация</button>
        </div>
        <div className="auth-heading">
          <h2 id="auth-form-title">{mode === "login" ? "С возвращением" : "Создать аккаунт"}</h2>
          <p>{mode === "login" ? "Продолжите работу с вашими проектами и чатами." : "После регистрации будет создано личное рабочее пространство."}</p>
        </div>
        <form className="auth-form" onSubmit={submit}>
          {mode === "register" ? (
            <label>
              <span>Имя</span>
              <input autoComplete="name" maxLength={80} onChange={(event) => setDisplayName(event.target.value)} required value={displayName} />
            </label>
          ) : null}
          <label>
            <span>Электронная почта</span>
            <input autoComplete="email" inputMode="email" maxLength={254} onChange={(event) => setEmail(event.target.value)} required type="email" value={email} />
          </label>
          <label>
            <span>Пароль</span>
            <input autoComplete={mode === "login" ? "current-password" : "new-password"} maxLength={128} minLength={mode === "register" ? 12 : 1} onChange={(event) => setPassword(event.target.value)} required type="password" value={password} />
            {mode === "register" ? <small>Не менее 12 символов; не используйте почту внутри пароля.</small> : null}
          </label>
          {mode === "register" ? (
            <label>
              <span>Повторите пароль</span>
              <input autoComplete="new-password" maxLength={128} minLength={12} onChange={(event) => setConfirmation(event.target.value)} required type="password" value={confirmation} />
            </label>
          ) : null}
          <div className="auth-error" aria-live="polite">{error}</div>
          <button className="auth-submit" disabled={submitting} type="submit">
            {submitting ? "Проверяем…" : mode === "login" ? "Войти" : "Зарегистрироваться"}
          </button>
        </form>
      </section>
    </main>
  );
}

function WorkspaceApp({ user, onLogout }: { user: AuthUser; onLogout: () => void }) {
  const [messages, setMessages] = useState<ChatEntry[]>([]);
  const [runningChatIds, setRunningChatIds] = useState<string[]>([]);
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
  const [managedProjectId, setManagedProjectId] = useState<string | null>(null);
  const activeChatIdRef = useRef("");
  const [workspaceSettings, setWorkspaceSettings] = useState<Record<string, unknown>>({});
  const [workspaceError, setWorkspaceError] = useState("");
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [uploadTarget, setUploadTarget] = useState<UploadTarget>("chat");
  const [isAttachmentPanelOpen, setIsAttachmentPanelOpen] = useState(false);
  const [uploadResult, setUploadResult] = useState<UploadResponse | null>(null);
  const [uploadError, setUploadError] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [documentsError, setDocumentsError] = useState("");
  const [isLoadingDocuments, setIsLoadingDocuments] = useState(false);
  const [deletingDocumentIds, setDeletingDocumentIds] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState("");
  const [modelError, setModelError] = useState("");
  const [isLoadingModels, setIsLoadingModels] = useState(true);
  const [isChangingModel, setIsChangingModel] = useState(false);
  const [activeProvider, setActiveProvider] = useState<ProviderName>("ollama");
  const [theme, setTheme] = useState<AppTheme>(() => window.localStorage.getItem(THEME_KEY) === "portal" ? "portal" : "default");
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(() => readStored(SIDEBAR_COLLAPSED_KEY, false));

  const managedProject = managedProjectId
    ? projects.find((project) => project.id === managedProjectId) ?? null
    : null;
  const activeChat = chats.find((chat) => chat.id === activeChatId) ?? null;
  const isRunning = Boolean(activeChatId && runningChatIds.includes(activeChatId));
  const hasRunningChats = runningChatIds.length > 0;
  const activeChatHasRunningMessage = messages.some(
    (message) => message.role === "assistant" && message.status === "running"
  );

  useEffect(() => {
    activeChatIdRef.current = activeChatId;
  }, [activeChatId]);

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
    if (activeChatIdRef.current === chatId) {
      const restored = panelStateFromMessageRecords(data.items);
      setMessages(data.items.map(messageFromRecord));
      setLastResult(restored.result);
      setLastQuestion(restored.question);
    }
    setRunningChatIds((current) => {
      const persistedRunning = hasRunningAssistantMessage(data.items);
      if (persistedRunning) {
        return current.includes(chatId) ? current : [...current, chatId];
      }
      return current.filter((runningChatId) => runningChatId !== chatId);
    });
  }, []);

  const loadDocuments = useCallback(async (projectId: string, chatId: string) => {
    if (!projectId && !chatId) {
      const shared = await getJson<{ items: DocumentRecord[] }>("/api/documents", API_TIMEOUT_MS);
      setDocuments(shared.items);
      return;
    }
    setIsLoadingDocuments(true);
    setDocumentsError("");
    try {
      const paths = ["/api/documents"];
      if (projectId) paths.push(`/api/projects/${projectId}/documents`);
      if (chatId) paths.push(`/api/documents?chat_id=${encodeURIComponent(chatId)}`);
      const responses = await Promise.all(paths.map((path) => (
        getJson<{ items: DocumentRecord[] }>(path, API_TIMEOUT_MS)
      )));
      const merged = new Map<string, DocumentRecord>();
      responses.flatMap((response) => response.items).forEach((document) => merged.set(document.id, document));
      setDocuments([...merged.values()]);
    } catch (error) {
      setDocumentsError(error instanceof Error ? error.message : "Не удалось загрузить список документов");
    } finally {
      setIsLoadingDocuments(false);
    }
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
      activeChatIdRef.current = nextChatId;
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

  useEffect(() => {
    void loadDocuments(activeProjectId, activeChatId);
  }, [activeChatId, activeProjectId, loadDocuments]);

  useEffect(() => {
    setUploadResult(null);
    setUploadError("");
    setSelectedFiles([]);
    setIsAttachmentPanelOpen(false);
    setUploadTarget("chat");
  }, [activeChatId, activeProjectId]);

  const setFilesForUploadTarget = useCallback((target: UploadTarget, files: File[]) => {
    setUploadTarget(target);
    setSelectedFiles(files);
    setUploadResult(null);
    setUploadError("");
  }, []);

  useEffect(() => {
    if (!activeChatId || !activeChatHasRunningMessage) return;
    const timer = window.setInterval(() => {
      void loadMessages(activeChatId).catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [activeChatHasRunningMessage, activeChatId, loadMessages]);

  const changeModel = useCallback(async (model: string) => {
    if (!model || model === selectedModel || isChangingModel || hasRunningChats) return;
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
  }, [hasRunningChats, isChangingModel, selectedModel]);

  const createChat = useCallback(async (projectId: string) => {
    const record = await postJson<ChatRecord>(`/api/projects/${projectId}/chats`, { title: "Новый чат" }, API_TIMEOUT_MS);
    const chat = chatFromRecord(record);
    setChats((current) => [...current, chat]);
    setActiveProjectId(projectId);
    setActiveChatId(chat.id);
    activeChatIdRef.current = chat.id;
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
    activeChatIdRef.current = chat.id;
    await syncWorkspace(null, chat.id);
    resetThreadState();
    return chat;
  }, [resetThreadState, syncWorkspace]);

  const openChat = useCallback(async (chat: ChatSummary) => {
    setActiveProjectId(chat.projectId ?? "");
    setActiveChatId(chat.id);
    activeChatIdRef.current = chat.id;
    setLastResult(null);
    setLastQuestion("");
    setDebugResult(null);
    setDebugError("");
    setActiveTab("system");
    await syncWorkspace(chat.projectId, chat.id);
    await loadMessages(chat.id);
  }, [loadMessages, syncWorkspace]);

  const selectProject = useCallback(async (projectId: string) => {
    const chat = chats.find((item) => item.projectId === projectId);
    if (chat) {
      await openChat(chat);
      return;
    }
    await createChat(projectId);
  }, [chats, createChat, openChat]);

  const manageProject = useCallback(async (projectId: string) => {
    await selectProject(projectId);
    setManagedProjectId(projectId);
  }, [selectProject]);

  const createProject = useCallback(async () => {
    try {
      const record = await postJson<ProjectRecord>("/api/projects", {
        name: "Новый проект",
        memory: { text: "Память проекта пока пуста." }
      }, API_TIMEOUT_MS);
      setProjects((current) => [...current, projectFromRecord(record)]);
      await createChat(record.id);
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось создать проект");
    }
  }, [createChat]);

  const updateProjectMemory = useCallback(async (projectId: string, memory: string): Promise<boolean> => {
    const previous = projects;
    setProjects((current) => current.map((project) => project.id === projectId ? { ...project, memory } : project));
    try {
      await patchJson<ProjectRecord>(`/api/projects/${projectId}`, { memory: { text: memory } }, API_TIMEOUT_MS);
      return true;
    } catch (error) {
      setProjects(previous);
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось сохранить память проекта");
      return false;
    }
  }, [projects]);

  const updateProjectKnowledgeScope = useCallback(async (projectId: string, knowledgeScope: KnowledgeScope) => {
    const previous = projects;
    setProjects((current) => current.map((project) => project.id === projectId
      ? { ...project, knowledgeScope }
      : project));
    try {
      const record = await patchJson<ProjectRecord>(
        `/api/projects/${projectId}`,
        { knowledge_scope: knowledgeScope },
        API_TIMEOUT_MS
      );
      setProjects((current) => current.map((project) => project.id === projectId ? projectFromRecord(record) : project));
      await loadDocuments(projectId, activeChatId);
    } catch (error) {
      setProjects(previous);
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось изменить область знаний проекта");
    }
  }, [activeChatId, loadDocuments, projects]);

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
      setManagedProjectId((current) => current === projectId ? null : current);
      if (activeProjectId === projectId) {
        setActiveProjectId("");
        await syncWorkspace(null, activeChatId);
      }
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "Не удалось удалить проект");
    }
  }, [activeChatId, activeProjectId, projects, syncWorkspace]);

  const deleteDocument = useCallback(async (document: DocumentRecord) => {
    const scope = document.chat_id ? "из этого чата" : document.project_id ? "из проекта" : "из общей библиотеки";
    if (!window.confirm(`Удалить документ «${document.file_name}» ${scope}? Он перестанет участвовать в ответах.`)) return;
    setDeletingDocumentIds((current) => current.includes(document.id) ? current : [...current, document.id]);
    setDocumentsError("");
    try {
      await deleteJson<{ id: string; status: "deleting"; job_id: string }>(
        `/api/documents/${document.id}`,
        API_TIMEOUT_MS
      );
      setDocuments((current) => current.filter((item) => item.id !== document.id));
    } catch (error) {
      setDocumentsError(error instanceof Error ? error.message : "Не удалось удалить документ");
      await loadDocuments(activeProjectId, activeChatId).catch(() => undefined);
    } finally {
      setDeletingDocumentIds((current) => current.filter((id) => id !== document.id));
    }
  }, [activeChatId, activeProjectId, loadDocuments]);

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
    const requestChatId = activeChatId;

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
    setRunningChatIds((current) => current.includes(requestChatId) ? current : [...current, requestChatId]);
    setLastResult(null);
    setLastQuestion(question);
    setDebugResult(null);
    setDebugError("");
    setActiveTab("system");

    let shouldClearRunning = false;
    try {
      await postJson<MessageRecord>(`/api/chats/${requestChatId}/messages`, { role: "user", content: question }, API_TIMEOUT_MS);
      const data = await postJson<AskResponse>("/api/ask", { question, chat_id: requestChatId }, ASK_TIMEOUT_MS);
      shouldClearRunning = generationResponseIsTerminal(data.status);

      const currentChat = chats.find((chat) => chat.id === requestChatId);
      if (currentChat && currentChat.title === "Новый чат") {
        const title = question.slice(0, 48);
        await patchJson<ChatRecord>(`/api/chats/${requestChatId}`, { title }, API_TIMEOUT_MS);
        setChats((current) => current.map((chat) => chat.id === requestChatId ? { ...chat, title } : chat));
      }

      if (activeChatIdRef.current === requestChatId) {
        setLastResult(data);
        await loadMessages(requestChatId);
      }
    } catch (error) {
      const text = error instanceof Error ? error.message : "Ошибка запроса";
      if (activeChatIdRef.current === requestChatId) {
        try {
          await loadMessages(requestChatId);
        } catch {
          setMessages((current) =>
            current.map((item) => item.id === assistantId ? { ...item, text, status: "error" } : item)
          );
        }
      }
    } finally {
      if (shouldClearRunning) {
        setRunningChatIds((current) => current.filter((chatId) => chatId !== requestChatId));
      }
    }
  }, [activeChatId, chats, loadMessages]);

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

  const uploadFiles = useCallback(async (target: UploadTarget) => {
    if (target !== uploadTarget || !selectedFiles.length || isUploading) return;
    const form = new FormData();
    selectedFiles.forEach((file) => form.append("files", file));
    try {
      const fields = uploadScopeFields(target, activeProjectId, activeChatId);
      Object.entries(fields).forEach(([key, value]) => form.append(key, value));
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "Не удалось определить область загрузки.");
      return;
    }

    setIsUploading(true);
    setUploadError("");
    setUploadResult(null);

    try {
      const data = await postFormData<UploadResponse>("/api/upload", form, UPLOAD_TIMEOUT_MS);
      setUploadResult(data);
      setSelectedFiles([]);
      await loadDocuments(activeProjectId, activeChatId);
      const pendingJobs = data.files.map((file) => file.job_id).filter(Boolean);
      for (let attempt = 0; attempt < 150 && pendingJobs.length; attempt += 1) {
        const states = await Promise.all(pendingJobs.map((jobId) => getJson<{ status: string }>(`/api/jobs/${jobId}`, API_TIMEOUT_MS)));
        if (states.every((state) => ["completed", "failed", "cancelled"].includes(state.status))) break;
        await new Promise((resolve) => window.setTimeout(resolve, 2_000));
      }
      await loadDocuments(activeProjectId, activeChatId);
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "Ошибка загрузки файлов");
    } finally {
      setIsUploading(false);
    }
  }, [activeChatId, activeProjectId, isUploading, loadDocuments, selectedFiles, uploadTarget]);

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
            activeChatId={activeChatId}
            activeProjectId={activeProjectId}
            chats={chats}
            documents={documents}
            documentsError={documentsError}
            deletingDocumentIds={deletingDocumentIds}
            isLoadingDocuments={isLoadingDocuments}
            pinnedChatIds={pinnedChatIds}
            pinnedProjectIds={pinnedProjectIds}
            projects={projects}
            onChatChange={(chat) => void openChat(chat)}
            onChatMove={moveChat}
            onChatDelete={(chatId) => void deleteChat(chatId)}
            onChatPin={(chatId) => void updatePinnedItems("chat", chatId)}
            onChatRename={(chatId, title) => void renameChat(chatId, title)}
            onDocumentDelete={(document) => void deleteDocument(document)}
            onProjectChange={(projectId) => void selectProject(projectId)}
            onProjectChatCreate={(projectId) => void createChat(projectId)}
            onProjectCreate={() => void createProject()}
            onProjectDelete={(projectId) => void deleteProject(projectId)}
            onProjectManage={(projectId) => void manageProject(projectId)}
            onProjectPin={(projectId) => void updatePinnedItems("project", projectId)}
            onProjectRename={(projectId, name) => void renameProject(projectId, name)}
            canDeleteGlobalDocuments={user.can_manage_global_documents}
          />
          {workspaceError && <div className="sidebar-note">{workspaceError}</div>}
          <div className="sidebar-user">
            <div className="sidebar-user-avatar" aria-hidden="true">{user.display_name.slice(0, 1).toUpperCase()}</div>
            <div className="sidebar-user-copy">
              <strong>{user.display_name}</strong>
              <span>{user.email}</span>
            </div>
            <button aria-label="Выйти" onClick={onLogout} title="Выйти" type="button"><LogOut size={16} /></button>
          </div>
        </aside>

        <section className="chat-surface">
          <Thread
            activeChatTitle={activeChat?.title || "Текущий чат"}
            documents={documents}
            error={uploadTarget === "chat" ? uploadError : ""}
            isAttachmentPanelOpen={isAttachmentPanelOpen}
            isUploading={isUploading && uploadTarget === "chat"}
            onAttachmentPanelChange={(open) => {
              if (open) setFilesForUploadTarget("chat", []);
              setIsAttachmentPanelOpen(open);
            }}
            onFilesChange={(files) => setFilesForUploadTarget("chat", files)}
            onUpload={() => void uploadFiles("chat")}
            result={uploadTarget === "chat" ? uploadResult : null}
            selectedFiles={uploadTarget === "chat" ? selectedFiles : []}
          />
        </section>

        <EvidencePanel
          activeTab={activeTab}
          activeProvider={activeProvider}
          canManageGlobalDocuments={user.can_manage_global_documents}
          theme={theme}
          debugError={debugError}
          debugResult={debugResult}
          isDebugging={isDebugging}
          isChangingModel={isChangingModel}
          isLoadingModels={isLoadingModels}
          isRunning={hasRunningChats}
          lastQuestion={lastQuestion}
          modelError={modelError}
          models={models}
          result={lastResult}
          selectedModel={selectedModel}
          onChangeModel={changeModel}
          onDebug={runDebug}
          error={uploadTarget === "global" ? uploadError : ""}
          documents={documents}
          documentsError={documentsError}
          isLoadingDocuments={isLoadingDocuments}
          isUploading={isUploading && uploadTarget === "global"}
          onProviderSettingsChange={setActiveProvider}
          onRefreshModels={loadModels}
          onTabChange={setActiveTab}
          onThemeChange={setTheme}
          selectedFiles={uploadTarget === "global" ? selectedFiles : []}
          uploadResult={uploadTarget === "global" ? uploadResult : null}
          onFilesChange={(files) => setFilesForUploadTarget("global", files)}
          onUpload={() => void uploadFiles("global")}
        />
        {managedProject ? (
          <ProjectWorkspacePanel
            deletingDocumentIds={deletingDocumentIds}
            documents={documents}
            documentsError={documentsError}
            error={uploadTarget === "project" ? uploadError : ""}
            isLoadingDocuments={isLoadingDocuments}
            isUploading={isUploading && uploadTarget === "project"}
            onClose={() => setManagedProjectId(null)}
            onDocumentDelete={(document) => void deleteDocument(document)}
            onFilesChange={(files) => setFilesForUploadTarget("project", files)}
            onKnowledgeScopeChange={(scope) => void updateProjectKnowledgeScope(managedProject.id, scope)}
            onMemorySave={(memory) => updateProjectMemory(managedProject.id, memory)}
            onUpload={() => void uploadFiles("project")}
            project={managedProject}
            result={uploadTarget === "project" ? uploadResult : null}
            selectedFiles={uploadTarget === "project" ? selectedFiles : []}
            theme={theme}
          />
        ) : null}
      </main>
    </AssistantRuntimeProvider>
  );
}

function NavigationSidebar({
  activeChatId,
  activeProjectId,
  chats,
  documents,
  documentsError,
  deletingDocumentIds,
  isLoadingDocuments,
  pinnedChatIds,
  pinnedProjectIds,
  projects,
  onChatChange,
  onChatDelete,
  onChatMove,
  onChatPin,
  onChatRename,
  onDocumentDelete,
  onProjectChange,
  onProjectChatCreate,
  onProjectCreate,
  onProjectDelete,
  onProjectManage,
  onProjectPin,
  onProjectRename,
  canDeleteGlobalDocuments
}: {
  activeChatId: string;
  activeProjectId: string;
  chats: ChatSummary[];
  documents: DocumentRecord[];
  documentsError: string;
  deletingDocumentIds: string[];
  isLoadingDocuments: boolean;
  pinnedChatIds: string[];
  pinnedProjectIds: string[];
  projects: Project[];
  onChatChange: (chat: ChatSummary) => void;
  onChatDelete: (chatId: string) => void;
  onChatMove: (chatId: string, projectId: string) => Promise<boolean>;
  onChatPin: (chatId: string) => void;
  onChatRename: (chatId: string, title: string) => void;
  onDocumentDelete: (document: DocumentRecord) => void;
  onProjectChange: (projectId: string) => void;
  onProjectChatCreate: (projectId: string) => void;
  onProjectCreate: () => void;
  onProjectDelete: (projectId: string) => void;
  onProjectManage: (projectId: string) => void;
  onProjectPin: (projectId: string) => void;
  onProjectRename: (projectId: string, name: string) => void;
  canDeleteGlobalDocuments: boolean;
}) {
  const [showAllProjects, setShowAllProjects] = useState(false);
  const [expandedProjectIds, setExpandedProjectIds] = useState<string[]>([]);
  const [expandedChatScopes, setExpandedChatScopes] = useState<string[]>([]);
  const [movingChatId, setMovingChatId] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ kind: "project" | "chat"; id: string; value: string } | null>(null);
  const visibleProjects = showAllProjects ? projects : projects.slice(0, 5);
  const pinnedProjects = projects.filter((project) => pinnedProjectIds.includes(project.id));
  const pinnedChats = chats.filter((chat) => pinnedChatIds.includes(chat.id));
  const freeChats = chats.filter((chat) => chat.projectId === null);
  const projectNames = new Map(projects.map((project) => [project.id, project.name]));
  const groupedDocuments = splitDocumentsByScope(documents);

  useEffect(() => {
    if (activeProjectId) setExpandedProjectIds([activeProjectId]);
  }, [activeProjectId]);

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
        active={chat.id === activeChatId}
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

  const renderChatList = (items: ChatSummary[], scope: string, nested = false) => {
    const expanded = expandedChatScopes.includes(scope);
    const activeIndex = items.findIndex((chat) => chat.id === activeChatId);
    const visible = visibleNavigationChats(items, expanded, 4, activeIndex);
    return (
      <>
        {visible.map((chat) => renderChat(chat, nested))}
        {items.length > 4 ? (
          <button
            className="nav-list-toggle"
            onClick={() => setExpandedChatScopes((current) => current.includes(scope)
              ? current.filter((item) => item !== scope)
              : [...current, scope])}
            type="button"
          >
            {expanded ? "Свернуть" : `Ещё ${items.length - 4}`}
          </button>
        ) : null}
      </>
    );
  };

  const renderDocuments = (items: DocumentRecord[], emptyCopy: string) => (
    <div className="nav-document-list" aria-live="polite">
      {!isLoadingDocuments && !documentsError && !items.length ? <div className="sidebar-empty">{emptyCopy}</div> : null}
      {items.map((document) => (
        <div className="nav-document-row" key={document.id}>
          <a
            className="nav-document"
            href={`/api/documents/${document.id}/content`}
            rel="noreferrer"
            target="_blank"
            title={`${document.file_name}. ${documentUploadedLabel(document.created_at)}. ${documentVersionLabel(document)}`}
          >
            {/\.(?:png|jpe?g|webp|tiff?|bmp)$/i.test(document.file_name) ? <ImageIcon size={15} /> : <FileText size={15} />}
            <span className="nav-document-copy">
              <span className="nav-document-name">{document.file_name}</span>
              <span className="nav-document-meta">
                {documentUploadedLabel(document.created_at)} · {document.chat_id ? "только этот чат" : documentVersionLabel(document)}
              </span>
            </span>
            <span className={`nav-document-status ${document.status}`}>
              {document.status === "ready"
                ? "Готов"
                : document.status === "processing"
                  ? `${document.processing_state?.progress ?? 0}%`
                  : document.status}
            </span>
          </a>
          {(document.scope !== "global" || canDeleteGlobalDocuments) ? (
            <button
              aria-label={`Удалить документ ${document.file_name}`}
              className="nav-document-delete"
              disabled={deletingDocumentIds.includes(document.id)}
              onClick={() => onDocumentDelete(document)}
              title="Удалить документ"
              type="button"
            >
              {deletingDocumentIds.includes(document.id) ? <RefreshCw className="spin" size={14} /> : <Trash2 size={14} />}
            </button>
          ) : null}
        </div>
      ))}
    </div>
  );

  return (
    <nav className="navigation-sidebar" aria-label="Проекты и чаты">
      {pinnedProjects.length || pinnedChats.length ? (
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
              onManageProject={() => onProjectManage(project.id)}
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
      ) : null}

      <section className="nav-section">
        <div className="nav-section-header">
          <div className="nav-section-title">Проекты</div>
          <button className="nav-add-project" onClick={onProjectCreate} title="Новый проект" type="button"><Plus size={15} /></button>
        </div>
        <div className="project-list">
          {visibleProjects.map((project) => (
            <div className={`project-tree ${expandedProjectIds.includes(project.id) ? "" : "is-collapsed"}`} key={project.id}>
              <NavigationItem
                active={project.id === activeProjectId}
                detail={`${chats.filter((chat) => chat.projectId === project.id).length} чатов`}
                icon={<Folder size={17} />}
                label={project.name}
                onCreateChat={() => onProjectChatCreate(project.id)}
                onDelete={() => onProjectDelete(project.id)}
                onManageProject={() => onProjectManage(project.id)}
                onOpen={() => {
                  setExpandedProjectIds((current) => current.includes(project.id)
                    ? current.filter((id) => id !== project.id)
                    : [project.id]);
                  onProjectChange(project.id);
                }}
                onPin={() => onProjectPin(project.id)}
                pinned={pinnedProjectIds.includes(project.id)}
                {...editProps("project", project.id, project.name)}
              />
              {expandedProjectIds.includes(project.id) ? (
                <div className="project-chat-list">
                  {renderChatList(chats.filter((chat) => chat.projectId === project.id), `project:${project.id}`, true)}
                </div>
              ) : null}
            </div>
          ))}
        </div>
        {projects.length > 5 ? (
          <button className="show-more" onClick={() => setShowAllProjects((value) => !value)} type="button">
            {showAllProjects ? "Скрыть" : "Показать еще"}
          </button>
        ) : null}
      </section>

      <section className="nav-section documents-section" aria-label="Общие документы">
        <div className="nav-section-header">
          <div className="nav-section-title">Общие документы</div>
          <span className="nav-count" title="Количество общих документов">{groupedDocuments.global.length}</span>
        </div>
        {isLoadingDocuments ? <div className="sidebar-empty">Обновляем список…</div> : null}
        {documentsError ? <div className="sidebar-empty error-copy">{documentsError}</div> : null}
        {renderDocuments(groupedDocuments.global, "Общая библиотека пока пуста.")}
      </section>

      {activeProjectId ? <section className="nav-section documents-section" aria-label="Документы проекта">
        <div className="nav-section-header">
          <div className="nav-section-title">Документы проекта</div>
          <span className="nav-count" title="Количество документов проекта">{groupedDocuments.project.length}</span>
        </div>
        {renderDocuments(groupedDocuments.project, "Документов проекта пока нет. Добавьте их через меню проекта.")}
      </section> : null}

      <section className="nav-section documents-section" aria-label="Документы чата">
        <div className="nav-section-header">
          <div className="nav-section-title">Документы чата</div>
          <span className="nav-count" title="Количество документов текущего чата">{groupedDocuments.chat.length}</span>
        </div>
        {renderDocuments(groupedDocuments.chat, "В этом чате пока нет файлов. Нажмите + возле поля вопроса.")}
      </section>

      {freeChats.length ? (
        <section className="nav-section chats-section">
          <div className="nav-section-title">Свободные чаты</div>
          <div className="sidebar-chat-list">{renderChatList(freeChats, "free")}</div>
        </section>
      ) : null}
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
  onManageProject,
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
  onManageProject?: () => void;
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
  const hasActionMenu = Boolean(onCreateChat || onManageProject || onMove || onPin || onRename || onDelete);

  const cancelCloseActionMenu = () => {
    if (closeMenuTimer.current !== null) {
      window.clearTimeout(closeMenuTimer.current);
      closeMenuTimer.current = null;
    }
  };

  const openActionMenu = (element: HTMLDivElement) => {
    cancelCloseActionMenu();
    const rect = element.getBoundingClientRect();
    setMenuAnchor({ top: rect.top, left: rect.right + 2 });
  };

  const closeActionMenu = (relatedTarget: EventTarget | null) => {
    if (relatedTarget instanceof Element && relatedTarget.closest(".nav-hover-menu")) {
      cancelCloseActionMenu();
      return;
    }

    if (relatedTarget instanceof Element && relatedTarget.closest(".nav-row")) {
      cancelCloseActionMenu();
      setMenuAnchor(null);
      return;
    }

    cancelCloseActionMenu();
    closeMenuTimer.current = window.setTimeout(() => {
      setMenuAnchor(null);
      closeMenuTimer.current = null;
    }, 140);
  };

  useEffect(() => () => cancelCloseActionMenu(), []);

  const actionMenu = !editing && hasActionMenu && menuAnchor && typeof document !== "undefined"
    ? createPortal(
      <div
        className="nav-hover-menu"
        onMouseEnter={cancelCloseActionMenu}
        onMouseLeave={() => {
          cancelCloseActionMenu();
          setMenuAnchor(null);
        }}
        role="menu"
        style={{ left: menuAnchor.left, top: menuAnchor.top }}
      >
        {onManageProject ? <button className="nav-menu-action" onClick={onManageProject} type="button"><Database size={14} /><span>Память и документы</span></button> : null}
        <button className="nav-menu-action" onClick={onOpen} type="button"><Settings2 size={14} /><span>{chat ? "Открыть чат" : "Открыть проект"}</span></button>
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
      onBlur={(event) => closeActionMenu(event.relatedTarget)}
      onFocus={(event) => hasActionMenu && openActionMenu(event.currentTarget)}
      onMouseEnter={(event) => hasActionMenu && openActionMenu(event.currentTarget)}
      onMouseLeave={(event) => closeActionMenu(event.relatedTarget)}
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
  documents,
  documentsError,
  isLoadingDocuments,
  isUploading,
  result,
  selectedFiles,
  target,
  onFilesChange,
  onUpload
}: {
  error: string;
  documents: DocumentRecord[];
  documentsError: string;
  isLoadingDocuments: boolean;
  isUploading: boolean;
  result: UploadResponse | null;
  selectedFiles: File[];
  target: "global" | "project";
  onFilesChange: (files: File[]) => void;
  onUpload: () => void;
}) {
  const inputId = `${target}-knowledge-file-input`;
  const feedback = uploadFeedback(result, documents);
  const groupedDocuments = splitDocumentsByScope(documents);
  const isGlobal = target === "global";
  const title = isGlobal ? "Общая база знаний" : "Загрузить в проект";
  const scopeNote = isGlobal
    ? "Эти файлы будут доступны всем пользователям во всех проектах."
    : "Эти файлы будут доступны во всех чатах текущего проекта.";
  const documentCount = isGlobal ? groupedDocuments.global.length : groupedDocuments.project.length;

  return (
    <section className="knowledge-loader" aria-label={title}>
      <div className="loader-title">
        <Database size={15} />
        <span>{title}</span>
      </div>
      <p className="loader-scope-note">{scopeNote}</p>
      <label className="file-picker" htmlFor={inputId}>
        <UploadCloud size={17} />
        <span>{selectedFiles.length ? `Выбрано: ${selectedFiles.length}` : "Выбрать документы или изображения"}</span>
      </label>
      <input
        accept=".docx,.pdf,.md,.txt,.png,.jpg,.jpeg,.webp,.tif,.tiff,.bmp,image/png,image/jpeg,image/webp,image/tiff,image/bmp"
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
        {isUploading ? "Распознаём и индексируем…" : isGlobal ? "Загрузить в общую базу" : "Загрузить в проект"}
      </button>

      {feedback ? (
        <div className={`upload-status ${feedback.state}`} aria-live="polite">
          {feedback.state === "error" ? (
            <AlertTriangle size={14} />
          ) : feedback.state === "pending" ? (
            <RefreshCw className="spin" size={14} />
          ) : (
            <CheckCircle2 size={14} />
          )}
          <span>{feedback.message}</span>
        </div>
      ) : null}
      {error ? (
        <div className="upload-status error">
          <AlertTriangle size={14} />
          <span>{error}</span>
        </div>
      ) : null}
      <div className="knowledge-summary" aria-live="polite">
        <Files size={14} />
        <span>
          {isLoadingDocuments
            ? "Обновляем список документов…"
            : `${documentCount} ${isGlobal ? "общих документов" : "документов проекта"}`}
        </span>
      </div>
      {documentsError ? (
        <div className="upload-status error"><AlertTriangle size={14} /><span>{documentsError}</span></div>
      ) : null}
    </section>
  );
}

function ProjectWorkspacePanel({
  project,
  documents,
  documentsError,
  error,
  isLoadingDocuments,
  isUploading,
  deletingDocumentIds,
  result,
  selectedFiles,
  theme,
  onClose,
  onDocumentDelete,
  onFilesChange,
  onKnowledgeScopeChange,
  onMemorySave,
  onUpload
}: {
  project: Project;
  documents: DocumentRecord[];
  documentsError: string;
  error: string;
  isLoadingDocuments: boolean;
  isUploading: boolean;
  deletingDocumentIds: string[];
  result: UploadResponse | null;
  selectedFiles: File[];
  theme: AppTheme;
  onClose: () => void;
  onDocumentDelete: (document: DocumentRecord) => void;
  onFilesChange: (files: File[]) => void;
  onKnowledgeScopeChange: (scope: KnowledgeScope) => void;
  onMemorySave: (memory: string) => Promise<boolean>;
  onUpload: () => void;
}) {
  const [memoryDraft, setMemoryDraft] = useState(project.memory);
  const [memoryState, setMemoryState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const projectDocuments = splitDocumentsByScope(documents).project.filter((document) => document.project_id === project.id);

  useEffect(() => {
    setMemoryDraft(project.memory);
    setMemoryState("idle");
  }, [project.id, project.memory]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const saveMemory = async () => {
    setMemoryState("saving");
    const saved = await onMemorySave(memoryDraft.trim());
    setMemoryState(saved ? "saved" : "error");
  };

  return createPortal(
    <div className={`project-workspace-backdrop theme-${theme}`} onMouseDown={onClose} role="presentation">
      <section
        aria-labelledby="project-workspace-title"
        aria-modal="true"
        className="project-workspace-panel"
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <div className="project-workspace-heading">
          <div>
            <h2 id="project-workspace-title">Память и документы проекта</h2>
            <p>{project.name}</p>
          </div>
          <button aria-label="Закрыть память проекта" className="icon-button" onClick={onClose} title="Закрыть" type="button">
            <X size={18} />
          </button>
        </div>

        <div className="project-workspace-grid">
          <div className="project-memory-column">
            <section className="project-memory-editor">
              <div className="project-workspace-section-title"><Folder size={15} /><span>Память проекта</span></div>
              <p>Краткий постоянный контекст, который добавляется к вопросам во всех чатах проекта.</p>
              <label htmlFor="project-memory-text">Контекст проекта</label>
              <textarea
                id="project-memory-text"
                onChange={(event) => {
                  setMemoryDraft(event.target.value);
                  setMemoryState("idle");
                }}
                value={memoryDraft}
              />
              <div className="project-memory-actions">
                <span aria-live="polite">
                  {memoryState === "saving" ? "Сохраняем…" : memoryState === "saved" ? "Сохранено" : memoryState === "error" ? "Не удалось сохранить" : ""}
                </span>
                <button disabled={memoryDraft === project.memory || memoryState === "saving"} onClick={() => void saveMemory()} type="button">
                  <Save size={15} />
                  Сохранить память
                </button>
              </div>
            </section>

            <section className="project-scope-editor">
              <div className="project-workspace-section-title"><Database size={15} /><span>Источники ответов</span></div>
              <label htmlFor="project-knowledge-scope">Использовать</label>
              <select
                id="project-knowledge-scope"
                onChange={(event) => onKnowledgeScopeChange(event.target.value as KnowledgeScope)}
                value={project.knowledgeScope}
              >
                <option value="project_and_global">Документы проекта и общие</option>
                <option value="project_only">Только документы проекта</option>
              </select>
              <p>
                {project.knowledgeScope === "project_only"
                  ? "Общая база отключена для этого проекта."
                  : "RAG ищет одновременно в документах проекта и общей базе."}
              </p>
            </section>
          </div>

          <div className="project-documents-column">
            <section className="project-document-registry" aria-label="Документы проекта">
              <div className="project-document-registry-heading">
                <div className="project-workspace-section-title"><Files size={15} /><span>Документы проекта</span></div>
                <span>{projectDocuments.length}</span>
              </div>
              {isLoadingDocuments ? <p className="project-document-empty">Обновляем список документов…</p> : null}
              {documentsError ? <p className="project-document-empty error-copy">{documentsError}</p> : null}
              {!isLoadingDocuments && !documentsError && !projectDocuments.length ? (
                <p className="project-document-empty">В проекте пока нет документов.</p>
              ) : null}
              {projectDocuments.length ? (
                <div className="project-document-list">
                  {projectDocuments.map((document) => (
                    <div className="project-document-row" key={document.id}>
                      <a href={`/api/documents/${document.id}/content`} rel="noreferrer" target="_blank">
                        {/\.(?:png|jpe?g|webp|tiff?|bmp)$/i.test(document.file_name) ? <ImageIcon size={16} /> : <FileText size={16} />}
                        <span>
                          <strong>{document.file_name}</strong>
                          <small>{documentUploadedLabel(document.created_at)} · {documentVersionLabel(document)}</small>
                        </span>
                        <em className={`nav-document-status ${document.status}`}>
                          {document.status === "ready" ? "Готов" : document.status === "processing" ? `${document.processing_state?.progress ?? 0}%` : document.status}
                        </em>
                      </a>
                      <button
                        aria-label={`Удалить документ ${document.file_name}`}
                        disabled={deletingDocumentIds.includes(document.id)}
                        onClick={() => onDocumentDelete(document)}
                        title="Удалить документ проекта"
                        type="button"
                      >
                        {deletingDocumentIds.includes(document.id) ? <RefreshCw className="spin" size={15} /> : <Trash2 size={15} />}
                      </button>
                    </div>
                  ))}
                </div>
              ) : null}
            </section>

            <KnowledgeLoader
              documents={documents}
              documentsError={documentsError}
              error={error}
              isLoadingDocuments={isLoadingDocuments}
              isUploading={isUploading}
              onFilesChange={onFilesChange}
              onUpload={onUpload}
              result={result}
              selectedFiles={selectedFiles}
              target="project"
            />
          </div>
        </div>
      </section>
    </div>,
    document.body
  );
}

function Thread({
  activeChatTitle,
  documents,
  error,
  isAttachmentPanelOpen,
  isUploading,
  onAttachmentPanelChange,
  onFilesChange,
  onUpload,
  result,
  selectedFiles
}: {
  activeChatTitle: string;
  documents: DocumentRecord[];
  error: string;
  isAttachmentPanelOpen: boolean;
  isUploading: boolean;
  onAttachmentPanelChange: (open: boolean) => void;
  onFilesChange: (files: File[]) => void;
  onUpload: () => void;
  result: UploadResponse | null;
  selectedFiles: File[];
}) {
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
        <div className="composer-stack">
          {isAttachmentPanelOpen ? (
            <AttachmentPanel
              activeChatTitle={activeChatTitle}
              documents={documents}
              error={error}
              isUploading={isUploading}
              onClose={() => onAttachmentPanelChange(false)}
              onFilesChange={onFilesChange}
              onUpload={onUpload}
              result={result}
              selectedFiles={selectedFiles}
            />
          ) : null}
          <Composer
            attachmentPanelOpen={isAttachmentPanelOpen}
            onAttach={() => {
              onAttachmentPanelChange(!isAttachmentPanelOpen);
            }}
          />
        </div>
      </ThreadPrimitive.ViewportFooter>
    </ThreadPrimitive.Root>
  );
}

function Message() {
  const role = useMessage((state) => state.role);
  const modelLabel = useMessage((state) => state.metadata.custom.modelLabel);
  const sources = useMessage((state) => state.metadata.custom.sources as ApiSource[] | undefined);
  const text = useMessage((state) => state.content.map((part) => part.type === "text" ? part.text : "").join(""));
  const isAssistant = role === "assistant";
  const [copied, setCopied] = useState(false);

  const copyMessage = async () => {
    await copyText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  return (
    <MessagePrimitive.Root className={`message-row ${isAssistant ? "assistant" : "user"}`}>
      <div className="message-body">
        {isAssistant && typeof modelLabel === "string" && modelLabel ? (
          <div className="model-badge">Ответил: {modelLabel}</div>
        ) : null}
        {isAssistant ? (
          <MarkdownAnswer markdown={text} sources={sources} />
        ) : (
          <MessagePrimitive.Content />
        )}
        <div className="message-actions">
          <button className="icon-button" type="button" onClick={() => void copyMessage()} title="Копировать сообщение" aria-label="Копировать сообщение">
            <Copy size={14} />
          </button>
          <span className={`message-copy-feedback ${copied ? "visible" : ""}`} aria-live="polite">Скопировано</span>
        </div>
      </div>
    </MessagePrimitive.Root>
  );
}

function MarkdownAnswer({ markdown, sources = [] }: { markdown: string; sources?: ApiSource[] }) {
  return (
    <ReactMarkdown
      className="answer-markdown"
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeRaw, rehypeSanitize, rehypeKatex]}
      components={{ table: ({ children }) => <MarkdownTable>{children}</MarkdownTable> }}
    >
      {normalizeMarkdown(normalizeSourceCitations(markdown, sources))}
    </ReactMarkdown>
  );
}

const legacyLatexCommand = /\\(?:mathrm|text|frac|sqrt|sum|prod|min|max|Delta|cdot|times)\b/;

function plainFormulaToLatex(formula: string): string {
  return formula
    .replace(
      /(?<![\\\p{L}\p{N}_])([A-Za-zА-Яа-яЁёΔЦ][A-Za-zА-Яа-яЁё0-9]*)(?:_sub)?\(([^()\n]{1,180})\)/gu,
      (_whole, symbol: string, rawIndices: string) => {
        const renderedSymbol = /[А-Яа-яЁё]/.test(symbol) ? `\\text{${symbol}}` : symbol;
        const indices = rawIndices
          .split(",")
          .map((value) => value.trim().replace(/[{}_]/g, " ").trim())
          .filter(Boolean)
          .map((value) => /[А-Яа-яЁё]/.test(value) ? `\\text{${value}}` : value)
          .join(",");
        return `${renderedSymbol}_{${indices}}`;
      }
    )
    .replace(/\s*[×*]\s*/g, " \\times ")
    .trim();
}

function normalizeLegacyLatexLine(line: string): string {
  line = line.replace(/`([^`\n]{1,500}=[^`\n]{1,500})`/g, (_whole, formula: string) => (
    `$${plainFormulaToLatex(formula)}$`
  ));
  if (line.includes("$") || !line.includes("=") || !legacyLatexCommand.test(line)) return line;
  const match = line.match(/^(\s*(?:[-*+]\s+)?)(.+)$/);
  if (!match) return line;
  const formula = match[2]
    .replace(/\\mathrm\{([^{}]*)\}/g, (_whole, label: string) => `\\text{${label}}`)
    .replace(/\\_/g, "_")
    .replace(/\\ /g, " ")
    .replace(/\\\./g, ".")
    .trim();
  return `${match[1]}$${formula}$`;
}

export function normalizeMarkdown(markdown: string): string {
  const normalized = markdown
    .replace(/\\?<(?:\/)?br\s*\/?>|&lt;\/?br\s*\/?&gt;/gi, (tag, offset, source) => {
      const lineStart = source.lastIndexOf("\n", offset) + 1;
      const lineEnd = source.indexOf("\n", offset);
      const line = source.slice(lineStart, lineEnd === -1 ? source.length : lineEnd);
      return line.includes("|") ? "<br />" : "\n";
    })
    .replace(/&nbsp;/gi, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  let insideCodeFence = false;
  let insideDisplayMath = false;
  return normalized.split("\n").map((line) => {
    if (/^\s*```/.test(line)) {
      insideCodeFence = !insideCodeFence;
      return line;
    }
    if (insideCodeFence) return line;
    if (/^\s*\$\$\s*$/.test(line)) {
      insideDisplayMath = !insideDisplayMath;
      return line;
    }
    const tableSafeLine = line.includes("|")
      ? line.replace(/(\S)\s+•\s*/g, "$1<br />• ")
      : line;
    if (insideDisplayMath) {
      // A valid display block already supplies the delimiters. Do not let the
      // legacy raw-LaTeX normalizer add inline dollars to its body, and unwrap
      // an extra pair copied by the model inside the block.
      return tableSafeLine
        .replace(/^(\s*)\${1,2}\s*/, "$1")
        .replace(/\s*\${1,2}(\s*)$/, "$1");
    }
    // Qwen can copy an already-delimited formula from the retrieval prompt and
    // wrap it in another pair of dollar delimiters (for example
    // `$$$$S = V \\times C$$$$`). remark-math treats the outer pair as the
    // block delimiter and passes the inner dollars to KaTeX, which then renders
    // a red parse error. Collapse only runs of three or more dollars; ordinary
    // inline `$...$`, display `$$...$$`, and currency text stay untouched.
    const mathSafeLine = tableSafeLine.replace(/\${3,}/g, () => "$$");
    return normalizeLegacyLatexLine(mathSafeLine);
  }).join("\n");
}

function tableToTsv(table: HTMLTableElement | null): string {
  if (!table) return "";
  return Array.from(table.rows)
    .map((row) => Array.from(row.cells)
      .map((cell) => (cell.textContent || "").replace(/\s+/g, " ").trim())
      .join("\t"))
    .join("\n");
}

async function copyText(value: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.append(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  }
}

function MarkdownTable({ children }: { children: ReactNode }) {
  const tableRef = useRef<HTMLTableElement>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    const tsv = tableToTsv(tableRef.current);
    if (!tsv) return;
    await copyText(tsv);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  return (
    <>
      <section className="markdown-table">
        <div className="markdown-table-toolbar">
          <span>Таблица</span>
          <div>
            <button type="button" onClick={() => setIsOpen(true)} title="Открыть таблицу полностью" aria-label="Открыть таблицу полностью">
              <Maximize2 size={14} aria-hidden="true" />
            </button>
            <button type="button" onClick={() => void copy()} title={copied ? "Скопировано" : "Скопировать таблицу"} aria-label={copied ? "Скопировано" : "Скопировать таблицу"}>
              {copied ? <CheckCircle2 size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
            </button>
          </div>
        </div>
        <div className="markdown-table-scroll">
          <table ref={tableRef}>{children}</table>
        </div>
      </section>

      {isOpen ? createPortal(
        <div className="markdown-table-modal-backdrop" role="presentation" onMouseDown={() => setIsOpen(false)}>
          <section className="markdown-table-modal" role="dialog" aria-modal="true" aria-label="Таблица" onMouseDown={(event) => event.stopPropagation()}>
            <div className="markdown-table-modal-heading">
              <h2>Таблица</h2>
              <div>
                <button type="button" onClick={() => void copy()} title={copied ? "Скопировано" : "Скопировать таблицу"} aria-label={copied ? "Скопировано" : "Скопировать таблицу"}>
                  {copied ? <CheckCircle2 size={15} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}
                </button>
                <button type="button" onClick={() => setIsOpen(false)} aria-label="Закрыть" title="Закрыть"><X size={17} /></button>
              </div>
            </div>
            <div className="markdown-table-modal-content">
              <table>{children}</table>
            </div>
          </section>
        </div>,
        document.body,
      ) : null}
    </>
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

function Composer({ attachmentPanelOpen, onAttach }: { attachmentPanelOpen: boolean; onAttach: () => void }) {
  return (
    <ComposerPrimitive.Root className="composer">
      <button
        aria-expanded={attachmentPanelOpen}
        aria-label="Добавить документ или изображение"
        className="attach-button"
        onClick={onAttach}
        title="Добавить документ или изображение"
        type="button"
      >
        <Plus size={20} />
      </button>
      <ComposerPrimitive.Input
        className="composer-input"
        placeholder="Введите вопрос"
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
  activeProvider,
  canManageGlobalDocuments,
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
  onRefreshModels,
  onTabChange,
  onThemeChange,
  documents,
  documentsError,
  error,
  isLoadingDocuments,
  isUploading,
  selectedFiles,
  uploadResult,
  onFilesChange,
  onUpload
}: {
  activeTab: PanelTab;
  activeProvider: ProviderName;
  canManageGlobalDocuments: boolean;
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
  onRefreshModels: () => void;
  onTabChange: (tab: PanelTab) => void;
  onThemeChange: (theme: AppTheme) => void;
  documents: DocumentRecord[];
  documentsError: string;
  error: string;
  isLoadingDocuments: boolean;
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
          {canManageGlobalDocuments ? (
            <KnowledgeLoader
              documents={documents}
              documentsError={documentsError}
              error={error}
              isLoadingDocuments={isLoadingDocuments}
              isUploading={isUploading}
              result={uploadResult}
              selectedFiles={selectedFiles}
              target="global"
              onFilesChange={onFilesChange}
              onUpload={onUpload}
            />
          ) : (
            <section className="global-library-note">
              <div className="loader-title"><Database size={15} /><span>Общая база знаний</span></div>
              <p>Общие документы доступны для поиска во всех проектах. У этой учётной записи нет права загрузки и удаления.</p>
              <strong>{splitDocumentsByScope(documents).global.length} документов доступно</strong>
            </section>
          )}
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
              {source.url ? (
                <a href={source.url} rel="noreferrer" target="_blank">{source.file}</a>
              ) : (
                <span>{source.file}</span>
              )}
            </div>
            <div className="source-meta">
              <span>{sourceLocatorLabel(source)}</span>
            </div>
          </div>
        ))
      ) : (
        <div className="muted">Нет источников.</div>
      )}
    </PanelSection>
  );
}

function uploadedDocumentName(value: string): string {
  let name = value.replace(/^.*[\\/]/, "");
  while (/^[0-9a-f]{32}_/i.test(name)) name = name.slice(33);
  return name || "документ";
}

export function normalizeSourceCitations(markdown: string, sources: ApiSource[] = []): string {
  const byName = new Map<string, ApiSource[]>();
  sources.forEach((source) => {
    const key = uploadedDocumentName(source.file).toLocaleLowerCase("ru-RU");
    byName.set(key, [...(byName.get(key) || []), source]);
  });

  return markdown.replace(
    /\[Источник:\s*([^\];]+?)(?:\s*(?:—|;)\s*(?:раздел:\s*)?([^\]]+?))?\](?!\()/gi,
    (_match, rawFile: string, rawSection?: string) => {
      const name = uploadedDocumentName(rawFile.trim());
      const requestedSection = (rawSection || "").trim().replace(/[. ]+$/, "");
      const candidates = byName.get(name.toLocaleLowerCase("ru-RU")) || [];
      const source = candidates.find((item) => (
        requestedSection && (item.section || "").toLocaleLowerCase("ru-RU").includes(requestedSection.toLocaleLowerCase("ru-RU"))
      )) || candidates[0];
      const displayName = source?.file || name;
      const locator = sourceLocatorLabel(source || { section: requestedSection || undefined });
      const label = `Источник: ${displayName} — ${locator}`;
      return source?.url ? `[${label}](${source.url})` : `[${label}]`;
    }
  );
}

function AttachmentPanel({
  activeChatTitle,
  documents,
  error,
  isUploading,
  onClose,
  onFilesChange,
  onUpload,
  result,
  selectedFiles
}: {
  activeChatTitle: string;
  documents: DocumentRecord[];
  error: string;
  isUploading: boolean;
  onClose: () => void;
  onFilesChange: (files: File[]) => void;
  onUpload: () => void;
  result: UploadResponse | null;
  selectedFiles: File[];
}) {
  const feedback = uploadFeedback(result, documents);
  const addFiles = (incoming: File[]) => {
    const unique = new Map(selectedFiles.map((file) => [`${file.name}:${file.size}:${file.lastModified}`, file]));
    incoming.forEach((file) => unique.set(`${file.name}:${file.size}:${file.lastModified}`, file));
    onFilesChange([...unique.values()]);
  };

  return (
    <section className="attachment-panel" aria-label="Добавление файлов в текущий чат">
      <div className="attachment-heading">
        <div>
          <h2>Добавить в текущий чат</h2>
          <p>Файлы будут доступны только в чате «{activeChatTitle}».</p>
        </div>
        <button aria-label="Закрыть панель добавления" className="icon-button" onClick={onClose} title="Закрыть" type="button">
          <X size={17} />
        </button>
      </div>

      <div className="attachment-kinds">
        <label htmlFor="attachment-document-input">
          <FileText size={18} />
          <span><strong>Документ</strong><small>DOCX, PDF, MD или TXT</small></span>
        </label>
        <input
          accept=".docx,.pdf,.md,.txt"
          className="file-input"
          id="attachment-document-input"
          multiple
          onChange={(event) => {
            addFiles(Array.from(event.currentTarget.files ?? []));
            event.currentTarget.value = "";
          }}
          type="file"
        />
        <label htmlFor="attachment-image-input">
          <ImageIcon size={18} />
          <span><strong>Изображение</strong><small>PNG, JPG, WebP, TIFF или BMP</small></span>
        </label>
        <input
          accept=".png,.jpg,.jpeg,.webp,.tif,.tiff,.bmp,image/png,image/jpeg,image/webp,image/tiff,image/bmp"
          className="file-input"
          id="attachment-image-input"
          multiple
          onChange={(event) => {
            addFiles(Array.from(event.currentTarget.files ?? []));
            event.currentTarget.value = "";
          }}
          type="file"
        />
      </div>

      <p className="attachment-ocr-note">Текст на изображениях распознаётся локально на русском и английском.</p>
      {selectedFiles.length ? (
        <div className="attachment-files" aria-label="Выбранные файлы">
          {selectedFiles.map((file, index) => (
            <div key={`${file.name}-${file.size}-${file.lastModified}`}>
              {/\.(?:png|jpe?g|webp|tiff?|bmp)$/i.test(file.name) ? <ImageIcon size={15} /> : <FileText size={15} />}
              <span>{file.name}</span>
              <button
                aria-label={`Убрать ${file.name}`}
                onClick={() => onFilesChange(selectedFiles.filter((_, fileIndex) => fileIndex !== index))}
                title="Убрать файл"
                type="button"
              >
                <X size={14} />
              </button>
            </div>
          ))}
        </div>
      ) : null}

      <div className="attachment-actions">
        <button className="attachment-upload" disabled={!selectedFiles.length || isUploading} onClick={onUpload} type="button">
          <UploadCloud size={16} />
          {isUploading ? "Распознаём и индексируем…" : "Загрузить в чат"}
        </button>
      </div>
      {feedback ? <div className={`upload-status ${feedback.state}`} aria-live="polite">{feedback.message}</div> : null}
      {error ? <div className="upload-status error" role="alert">{error}</div> : null}
    </section>
  );
}

export function sourceLocatorLabel(source: Pick<ApiSource, "locator" | "section">): string {
  return source.locator || (source.section ? `Раздел «${source.section}»` : "Документ целиком");
}

export function documentUploadedLabel(createdAt?: string): string {
  if (!createdAt) return "Время загрузки неизвестно";
  const date = new Date(createdAt);
  if (Number.isNaN(date.getTime())) return "Время загрузки неизвестно";
  return `Загружен ${new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short"
  }).format(date)}`;
}

export function visibleNavigationChats<T>(items: T[], expanded: boolean, limit = 4, activeIndex = -1): T[] {
  if (expanded || items.length <= limit) return items;
  if (activeIndex < limit || activeIndex >= items.length) return items.slice(0, limit);
  return [...items.slice(0, Math.max(limit - 1, 0)), items[activeIndex]];
}

export function documentVersionLabel(
  document: Pick<DocumentRecord, "version_label" | "version_status" | "newer_file_name">
): string {
  if (!document.version_label) return "Версия не указана";
  if (document.version_status === "superseded") {
    return `Есть новее: ${document.newer_file_name || "другая загруженная версия"}`;
  }
  return `Версия ${document.version_label} · последняя из загруженных`;
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
