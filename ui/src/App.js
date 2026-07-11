import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
import { ActionBarPrimitive, AssistantRuntimeProvider, ComposerPrimitive, MessagePrimitive, ThreadPrimitive, useExternalStoreRuntime, useMessage } from "@assistant-ui/react";
import { AlertTriangle, Bot, Bug, CheckCircle2, Copy, Database, Cpu, FileText, Files, Folder, FolderInput, PanelRight, PanelLeftClose, PanelLeftOpen, Paintbrush, Pencil, Pin, Plus, RefreshCw, Save, Send, Settings2, Sigma, Sparkles, TestTube2, Trash2, UploadCloud, User, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { deleteJson, getJson, patchJson, postFormData, postJson } from "./api";
const DEFAULT_PROJECT = {
    id: "regulations",
    name: "Регламенты",
    memory: "Точный поиск по пунктам регламентов. Формулы и источники показывать явно."
};
const FREE_CHAT_WORKSPACE = {
    id: "",
    name: "Свободный чат",
    memory: "Контекст ограничен текущим чатом."
};
const THEME_KEY = "rag-assistant-theme";
const SIDEBAR_COLLAPSED_KEY = "rag-assistant-sidebar-collapsed";
const ASK_TIMEOUT_MS = 180000;
const DEBUG_TIMEOUT_MS = 60000;
const UPLOAD_TIMEOUT_MS = 300000;
const API_TIMEOUT_MS = 60000;
const createId = () => crypto.randomUUID();
function projectFromRecord(record) {
    const memory = typeof record.memory?.text === "string" ? record.memory.text : "";
    return { id: record.id, name: record.name, memory };
}
function chatFromRecord(record) {
    return { id: record.id, projectId: record.project_id, title: record.title, messages: [] };
}
function messageFromRecord(record) {
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
function readStored(key, fallback) {
    try {
        const value = window.localStorage.getItem(key);
        return value ? JSON.parse(value) : fallback;
    }
    catch {
        return fallback;
    }
}
const extractText = (message) => {
    const content = message.content;
    if (typeof content === "string")
        return content;
    return content
        .map((part) => {
        if (part.type === "text")
            return part.text;
        return "";
    })
        .join("")
        .trim();
};
const toThreadMessage = (message) => ({
    id: message.id,
    role: message.role,
    content: message.status === "running" && !message.text
        ? "Думаю..."
        : message.text,
    status: message.role === "assistant"
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
    let promise = null;
    return () => {
        if (typeof window === "undefined")
            return Promise.resolve();
        if (window.MathJax?.typesetPromise)
            return Promise.resolve();
        if (promise)
            return promise;
        window.MathJax = {
            startup: { typeset: false },
            svg: { fontCache: "global" }
        };
        promise = new Promise((resolve, reject) => {
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
function useMathTypeset(html) {
    const ref = useRef(null);
    useEffect(() => {
        if (!html || !ref.current)
            return;
        const element = ref.current;
        ensureMathJax()
            .then(() => window.MathJax?.typesetPromise?.([element]))
            .catch(() => undefined);
    }, [html]);
    return ref;
}
export function App() {
    const [messages, setMessages] = useState([]);
    const [isRunning, setIsRunning] = useState(false);
    const [lastResult, setLastResult] = useState(null);
    const [lastQuestion, setLastQuestion] = useState("");
    const [debugResult, setDebugResult] = useState(null);
    const [debugError, setDebugError] = useState("");
    const [isDebugging, setIsDebugging] = useState(false);
    const [activeTab, setActiveTab] = useState("system");
    const [projects, setProjects] = useState([]);
    const [activeProjectId, setActiveProjectId] = useState("");
    const [chats, setChats] = useState([]);
    const [activeChatId, setActiveChatId] = useState("");
    const [workspaceSettings, setWorkspaceSettings] = useState({});
    const [workspaceError, setWorkspaceError] = useState("");
    const [selectedFiles, setSelectedFiles] = useState([]);
    const [uploadResult, setUploadResult] = useState(null);
    const [uploadError, setUploadError] = useState("");
    const [isUploading, setIsUploading] = useState(false);
    const [models, setModels] = useState([]);
    const [selectedModel, setSelectedModel] = useState("");
    const [modelError, setModelError] = useState("");
    const [isLoadingModels, setIsLoadingModels] = useState(true);
    const [isChangingModel, setIsChangingModel] = useState(false);
    const [activeProvider, setActiveProvider] = useState("ollama");
    const [theme, setTheme] = useState(() => window.localStorage.getItem(THEME_KEY) === "portal" ? "portal" : "default");
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
    const loadMessages = useCallback(async (chatId) => {
        const data = await getJson(`/api/chats/${chatId}/messages`, API_TIMEOUT_MS);
        setMessages(data.items.map(messageFromRecord));
    }, []);
    const syncWorkspace = useCallback(async (projectId, chatId) => {
        const workspace = await postJson("/api/settings", {
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
                getJson("/api/workspace", API_TIMEOUT_MS),
                getJson("/api/projects", API_TIMEOUT_MS),
                getJson("/api/chats", API_TIMEOUT_MS)
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
            if (nextChatId)
                await loadMessages(nextChatId);
            else
                setMessages([]);
        }
        catch (error) {
            setWorkspaceError(error instanceof Error ? error.message : "Не удалось загрузить проекты и чаты");
        }
    }, [loadMessages]);
    const loadModels = useCallback(async () => {
        setIsLoadingModels(true);
        setModelError("");
        try {
            const data = await getJson("/api/models", API_TIMEOUT_MS);
            setModels(data.models);
            setSelectedModel(data.selected);
        }
        catch (error) {
            setModelError(error instanceof Error ? error.message : "Не удалось получить список моделей");
        }
        finally {
            setIsLoadingModels(false);
        }
    }, []);
    useEffect(() => {
        void loadModels();
        void loadWorkspace();
    }, [loadModels, loadWorkspace]);
    const changeModel = useCallback(async (model) => {
        if (!model || model === selectedModel || isChangingModel || isRunning)
            return;
        setIsChangingModel(true);
        setModelError("");
        try {
            const data = await postJson("/api/models/select", { model }, DEBUG_TIMEOUT_MS);
            setSelectedModel(data.model);
        }
        catch (error) {
            setModelError(error instanceof Error ? error.message : "Не удалось сменить модель");
        }
        finally {
            setIsChangingModel(false);
        }
    }, [isChangingModel, isRunning, selectedModel]);
    const createChat = useCallback(async (projectId) => {
        const record = await postJson(`/api/projects/${projectId}/chats`, { title: "Новый чат" }, API_TIMEOUT_MS);
        const chat = chatFromRecord(record);
        setChats((current) => [...current, chat]);
        setActiveProjectId(projectId);
        setActiveChatId(chat.id);
        await syncWorkspace(projectId, chat.id);
        resetThreadState();
        return chat;
    }, [resetThreadState, syncWorkspace]);
    const createFreeChat = useCallback(async () => {
        const record = await postJson("/api/chats", { title: "Новый чат" }, API_TIMEOUT_MS);
        const chat = chatFromRecord(record);
        setChats((current) => [...current, chat]);
        setActiveProjectId("");
        setActiveChatId(chat.id);
        await syncWorkspace(null, chat.id);
        resetThreadState();
        return chat;
    }, [resetThreadState, syncWorkspace]);
    const openChat = useCallback(async (chat) => {
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
    const selectProject = useCallback(async (projectId) => {
        const chat = chats.find((item) => item.projectId === projectId);
        if (chat) {
            await openChat(chat);
            return;
        }
        await createChat(projectId);
    }, [chats, createChat, openChat]);
    const createProject = useCallback(async () => {
        try {
            const record = await postJson("/api/projects", {
                name: "Новый проект",
                memory: { text: "Память проекта пока пуста." }
            }, API_TIMEOUT_MS);
            setProjects((current) => [...current, projectFromRecord(record)]);
            await createChat(record.id);
        }
        catch (error) {
            setWorkspaceError(error instanceof Error ? error.message : "Не удалось создать проект");
        }
    }, [createChat]);
    const updateProjectMemory = useCallback(async (memory) => {
        if (!activeProjectId)
            return;
        const previous = projects;
        setProjects((current) => current.map((project) => project.id === activeProjectId ? { ...project, memory } : project));
        try {
            await patchJson(`/api/projects/${activeProjectId}`, { memory: { text: memory } }, API_TIMEOUT_MS);
        }
        catch (error) {
            setProjects(previous);
            setWorkspaceError(error instanceof Error ? error.message : "Не удалось сохранить память проекта");
        }
    }, [activeProjectId, projects]);
    const renameProject = useCallback(async (projectId, name) => {
        const project = projects.find((item) => item.id === projectId);
        if (!project)
            return;
        if (!name || name === project.name)
            return;
        try {
            const record = await patchJson(`/api/projects/${projectId}`, { name }, API_TIMEOUT_MS);
            setProjects((current) => current.map((item) => item.id === projectId ? projectFromRecord(record) : item));
        }
        catch (error) {
            setWorkspaceError(error instanceof Error ? error.message : "Не удалось переименовать проект");
        }
    }, [projects]);
    const renameChat = useCallback(async (chatId, title) => {
        const chat = chats.find((item) => item.id === chatId);
        if (!chat)
            return;
        if (!title || title === chat.title)
            return;
        try {
            const record = await patchJson(`/api/chats/${chatId}`, { title }, API_TIMEOUT_MS);
            setChats((current) => current.map((item) => item.id === chatId ? chatFromRecord(record) : item));
        }
        catch (error) {
            setWorkspaceError(error instanceof Error ? error.message : "Не удалось переименовать чат");
        }
    }, [chats]);
    const deleteChat = useCallback(async (chatId) => {
        const chat = chats.find((item) => item.id === chatId);
        if (!chat || !window.confirm(`Удалить чат «${chat.title}»?`))
            return;
        try {
            await deleteJson(`/api/chats/${chatId}`, API_TIMEOUT_MS);
            setChats((current) => current.filter((item) => item.id !== chatId));
            if (activeChatId === chatId) {
                setActiveChatId("");
                resetThreadState();
                await syncWorkspace(null, "");
            }
        }
        catch (error) {
            setWorkspaceError(error instanceof Error ? error.message : "Не удалось удалить чат");
        }
    }, [activeChatId, chats, resetThreadState, syncWorkspace]);
    const deleteProject = useCallback(async (projectId) => {
        const project = projects.find((item) => item.id === projectId);
        if (!project || !window.confirm(`Удалить проект «${project.name}»? Его чаты станут свободными.`))
            return;
        try {
            await deleteJson(`/api/projects/${projectId}`, API_TIMEOUT_MS);
            setProjects((current) => current.filter((item) => item.id !== projectId));
            setChats((current) => current.map((item) => item.projectId === projectId ? { ...item, projectId: null } : item));
            if (activeProjectId === projectId) {
                setActiveProjectId("");
                await syncWorkspace(null, activeChatId);
            }
        }
        catch (error) {
            setWorkspaceError(error instanceof Error ? error.message : "Не удалось удалить проект");
        }
    }, [activeChatId, activeProjectId, projects, syncWorkspace]);
    const moveChat = useCallback(async (chatId, projectId) => {
        const chat = chats.find((item) => item.id === chatId);
        if (!chat)
            return false;
        try {
            const record = await patchJson(`/api/chats/${chatId}`, { project_id: projectId }, API_TIMEOUT_MS);
            setChats((current) => current.map((item) => item.id === chatId ? chatFromRecord(record) : item));
            if (activeChatId === chatId) {
                setActiveProjectId(projectId);
                await syncWorkspace(projectId, chatId);
            }
            return true;
        }
        catch (error) {
            setWorkspaceError(error instanceof Error ? error.message : "Не удалось перенести чат в проект");
            return false;
        }
    }, [activeChatId, chats, syncWorkspace]);
    const updatePinnedItems = useCallback(async (kind, itemId) => {
        const settingKey = kind === "project" ? "pinned_project_ids" : "pinned_chat_ids";
        const current = Array.isArray(workspaceSettings[settingKey])
            ? workspaceSettings[settingKey].filter((value) => typeof value === "string")
            : [];
        const nextIds = current.includes(itemId)
            ? current.filter((id) => id !== itemId)
            : [...current, itemId];
        const nextSettings = { ...workspaceSettings, [settingKey]: nextIds };
        try {
            const workspace = await postJson("/api/settings", {
                active_project_id: activeProjectId || null,
                active_chat_id: activeChatId || null,
                settings: nextSettings
            }, API_TIMEOUT_MS);
            setWorkspaceSettings(workspace.settings ?? nextSettings);
        }
        catch (error) {
            setWorkspaceError(error instanceof Error ? error.message : "Не удалось обновить закреплённые элементы");
        }
    }, [activeChatId, activeProjectId, workspaceSettings]);
    const pinnedProjectIds = useMemo(() => Array.isArray(workspaceSettings.pinned_project_ids)
        ? workspaceSettings.pinned_project_ids.filter((value) => typeof value === "string")
        : [], [workspaceSettings]);
    const pinnedChatIds = useMemo(() => Array.isArray(workspaceSettings.pinned_chat_ids)
        ? workspaceSettings.pinned_chat_ids.filter((value) => typeof value === "string")
        : [], [workspaceSettings]);
    const onNew = useCallback(async (message) => {
        const question = extractText(message);
        if (!question || !activeChatId)
            return;
        const userMessage = {
            id: createId(),
            role: "user",
            text: question,
            status: "complete"
        };
        const assistantId = createId();
        const assistantMessage = {
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
            await postJson(`/api/chats/${activeChatId}/messages`, { role: "user", content: question }, API_TIMEOUT_MS);
            const data = await postJson("/api/ask", { question, chat_id: activeChatId }, ASK_TIMEOUT_MS);
            await postJson(`/api/chats/${activeChatId}/messages`, {
                role: "assistant",
                content: data.answer || "Пустой ответ.",
                content_html: data.html_answer || "",
                status: "complete",
                metadata: { model_label: data.llm?.label, sources: data.sources, usage: data.usage }
            }, API_TIMEOUT_MS);
            const currentChat = chats.find((chat) => chat.id === activeChatId);
            if (currentChat && currentChat.title === "Новый чат") {
                const title = question.slice(0, 48);
                await patchJson(`/api/chats/${activeChatId}`, { title }, API_TIMEOUT_MS);
                setChats((current) => current.map((chat) => chat.id === activeChatId ? { ...chat, title } : chat));
            }
            setLastResult(data);
            setMessages((current) => current.map((item) => item.id === assistantId
                ? {
                    ...item,
                    text: data.answer || "Пустой ответ.",
                    html: data.html_answer,
                    modelLabel: data.llm?.label,
                    status: "complete"
                }
                : item));
        }
        catch (error) {
            const text = error instanceof Error ? error.message : "Ошибка запроса";
            void postJson(`/api/chats/${activeChatId}/messages`, {
                role: "assistant",
                content: text,
                status: "error"
            }, API_TIMEOUT_MS).catch(() => undefined);
            setMessages((current) => current.map((item) => item.id === assistantId
                ? {
                    ...item,
                    text,
                    status: "error"
                }
                : item));
        }
        finally {
            setIsRunning(false);
        }
    }, [activeChatId, chats]);
    const adapter = useMemo(() => ({
        messages,
        isRunning,
        onNew,
        setMessages: (next) => setMessages([...next]),
        convertMessage: toThreadMessage
    }), [messages, isRunning, onNew]);
    const runtime = useExternalStoreRuntime(adapter);
    const clearThread = useCallback(async () => {
        try {
            await createFreeChat();
        }
        catch (error) {
            setWorkspaceError(error instanceof Error ? error.message : "Не удалось создать чат");
        }
    }, [createFreeChat]);
    const runDebug = useCallback(async () => {
        if (!lastQuestion || isRunning || isDebugging)
            return;
        setIsDebugging(true);
        setDebugError("");
        setActiveTab("debug");
        try {
            const data = await postJson("/api/debug", { question: lastQuestion }, DEBUG_TIMEOUT_MS);
            setDebugResult(data);
        }
        catch (error) {
            setDebugError(error instanceof Error ? error.message : "Ошибка debug-запроса");
        }
        finally {
            setIsDebugging(false);
        }
    }, [isDebugging, isRunning, lastQuestion]);
    const uploadFiles = useCallback(async () => {
        if (!selectedFiles.length || isUploading)
            return;
        const form = new FormData();
        selectedFiles.forEach((file) => form.append("files", file));
        setIsUploading(true);
        setUploadError("");
        setUploadResult(null);
        try {
            const data = await postFormData("/api/upload", form, UPLOAD_TIMEOUT_MS);
            setUploadResult(data);
            setSelectedFiles([]);
        }
        catch (error) {
            setUploadError(error instanceof Error ? error.message : "Ошибка загрузки файлов");
        }
        finally {
            setIsUploading(false);
        }
    }, [isUploading, selectedFiles]);
    return (_jsx(AssistantRuntimeProvider, { runtime: runtime, children: _jsxs("main", { className: `app-shell theme-${theme} ${isSidebarCollapsed ? "sidebar-collapsed" : ""}`, children: [_jsxs("aside", { className: "sidebar", children: [_jsxs("div", { className: "sidebar-topbar", children: [_jsx("div", { className: "sidebar-title", children: "AI \u0418\u0410\u0421 \u042D\u043D\u0435\u0440\u0433\u043E\u0431\u0430\u043B\u0430\u043D\u0441" }), _jsx("button", { className: "sidebar-collapse", onClick: () => setIsSidebarCollapsed((value) => !value), title: isSidebarCollapsed ? "Развернуть левый сайдбар" : "Свернуть левый сайдбар", type: "button", children: isSidebarCollapsed ? _jsx(PanelLeftOpen, { size: 17 }) : _jsx(PanelLeftClose, { size: 17 }) })] }), _jsxs("button", { className: "new-thread", onClick: () => void clearThread(), type: "button", children: [_jsx("span", { className: "new-thread-icon", children: _jsx(Plus, { size: 16 }) }), _jsx("span", { children: "\u041D\u043E\u0432\u044B\u0439 \u0434\u0438\u0430\u043B\u043E\u0433" })] }), _jsx(NavigationSidebar, { activeProjectId: activeProjectId, chats: chats, pinnedChatIds: pinnedChatIds, pinnedProjectIds: pinnedProjectIds, projects: projects, onChatChange: (chat) => void openChat(chat), onChatMove: moveChat, onChatDelete: (chatId) => void deleteChat(chatId), onChatPin: (chatId) => void updatePinnedItems("chat", chatId), onChatRename: (chatId, title) => void renameChat(chatId, title), onProjectChange: (projectId) => void selectProject(projectId), onProjectChatCreate: (projectId) => void createChat(projectId), onProjectCreate: () => void createProject(), onProjectDelete: (projectId) => void deleteProject(projectId), onProjectPin: (projectId) => void updatePinnedItems("project", projectId), onProjectRename: (projectId, name) => void renameProject(projectId, name) }), workspaceError && _jsx("div", { className: "sidebar-note", children: workspaceError })] }), _jsx("section", { className: "chat-surface", children: _jsx(Thread, {}) }), _jsx(EvidencePanel, { activeTab: activeTab, activeProject: activeProject, activeProvider: activeProvider, theme: theme, debugError: debugError, debugResult: debugResult, isDebugging: isDebugging, isChangingModel: isChangingModel, isLoadingModels: isLoadingModels, isRunning: isRunning, lastQuestion: lastQuestion, modelError: modelError, models: models, result: lastResult, selectedModel: selectedModel, onChangeModel: changeModel, onDebug: runDebug, error: uploadError, isUploading: isUploading, onProviderSettingsChange: setActiveProvider, onProjectMemoryChange: (memory) => void updateProjectMemory(memory), onRefreshModels: loadModels, onTabChange: setActiveTab, onThemeChange: setTheme, selectedFiles: selectedFiles, uploadResult: uploadResult, onFilesChange: setSelectedFiles, onUpload: uploadFiles })] }) }));
}
function NavigationSidebar({ activeProjectId, chats, pinnedChatIds, pinnedProjectIds, projects, onChatChange, onChatDelete, onChatMove, onChatPin, onChatRename, onProjectChange, onProjectChatCreate, onProjectCreate, onProjectDelete, onProjectPin, onProjectRename }) {
    const [showAllProjects, setShowAllProjects] = useState(false);
    const [collapsedProjectIds, setCollapsedProjectIds] = useState([]);
    const [movingChatId, setMovingChatId] = useState(null);
    const [editing, setEditing] = useState(null);
    const visibleProjects = showAllProjects ? projects : projects.slice(0, 5);
    const pinnedProjects = projects.filter((project) => pinnedProjectIds.includes(project.id));
    const pinnedChats = chats.filter((chat) => pinnedChatIds.includes(chat.id));
    const freeChats = chats.filter((chat) => chat.projectId === null);
    const projectNames = new Map(projects.map((project) => [project.id, project.name]));
    const saveEdit = () => {
        if (!editing)
            return;
        const value = editing.value.trim();
        if (!value)
            return;
        if (editing.kind === "project")
            onProjectRename(editing.id, value);
        else
            onChatRename(editing.id, value);
        setEditing(null);
    };
    const editProps = (kind, id, value) => ({
        editing: editing?.kind === kind && editing.id === id,
        editValue: editing?.kind === kind && editing.id === id ? editing.value : value,
        onEditChange: (next) => setEditing((current) => current ? { ...current, value: next } : current),
        onEditCancel: () => setEditing(null),
        onEditSave: saveEdit,
        onRename: () => setEditing({ kind, id, value })
    });
    const renderChat = (chat, nested = false) => (_jsxs("div", { className: `nav-chat-group ${nested ? "project-chat" : ""}`, children: [_jsx(NavigationItem, { chat: true, label: chat.title, onDelete: () => onChatDelete(chat.id), onMove: () => setMovingChatId((current) => current === chat.id ? null : chat.id), onOpen: () => onChatChange(chat), onPin: () => onChatPin(chat.id), pinned: pinnedChatIds.includes(chat.id), ...editProps("chat", chat.id, chat.title) }), movingChatId === chat.id ? (_jsxs("div", { className: "nav-move-menu", children: [_jsx("div", { className: "nav-move-title", children: "\u041F\u0435\u0440\u0435\u043D\u0435\u0441\u0442\u0438 \u0432 \u043F\u0440\u043E\u0435\u043A\u0442" }), projects.map((project) => (_jsxs("button", { className: project.id === chat.projectId ? "nav-move-current" : undefined, onClick: () => void onChatMove(chat.id, project.id).then((moved) => {
                            if (moved)
                                setMovingChatId(null);
                        }), type: "button", children: [_jsx(Folder, { size: 14 }), _jsx("span", { children: project.name }), project.id === chat.projectId ? _jsx("small", { children: "\u0422\u0435\u043A\u0443\u0449\u0438\u0439 \u043F\u0440\u043E\u0435\u043A\u0442" }) : null] }, project.id)))] })) : null] }, chat.id));
    return (_jsxs("nav", { className: "navigation-sidebar", "aria-label": "\u041F\u0440\u043E\u0435\u043A\u0442\u044B \u0438 \u0447\u0430\u0442\u044B", children: [_jsxs("section", { className: "nav-section", children: [_jsx("div", { className: "nav-section-title", children: "\u0417\u0430\u043A\u0440\u0435\u043F\u043B\u0451\u043D\u043D\u044B\u0435" }), _jsxs("div", { className: "project-list pinned-list", children: [pinnedProjects.map((project) => (_jsx(NavigationItem, { active: project.id === activeProjectId, icon: _jsx(Folder, { size: 17 }), label: project.name, onCreateChat: () => onProjectChatCreate(project.id), onOpen: () => onProjectChange(project.id), onDelete: () => onProjectDelete(project.id), onPin: () => onProjectPin(project.id), onRename: () => setEditing({ kind: "project", id: project.id, value: project.name }), pinned: true }, project.id))), pinnedChats.map((chat) => (_jsx(NavigationItem, { chat: true, detail: chat.projectId ? projectNames.get(chat.projectId) : undefined, label: chat.title, onDelete: () => onChatDelete(chat.id), onMove: () => setMovingChatId((current) => current === chat.id ? null : chat.id), onOpen: () => onChatChange(chat), onPin: () => onChatPin(chat.id), onRename: () => setEditing({ kind: "chat", id: chat.id, value: chat.title }), pinned: true }, chat.id)))] })] }), _jsxs("section", { className: "nav-section", children: [_jsxs("div", { className: "nav-section-header", children: [_jsx("div", { className: "nav-section-title", children: "\u041F\u0440\u043E\u0435\u043A\u0442\u044B" }), _jsx("button", { className: "nav-add-project", onClick: onProjectCreate, title: "\u041D\u043E\u0432\u044B\u0439 \u043F\u0440\u043E\u0435\u043A\u0442", type: "button", children: _jsx(Plus, { size: 15 }) })] }), _jsx("div", { className: "project-list", children: visibleProjects.map((project) => (_jsxs("div", { className: `project-tree ${collapsedProjectIds.includes(project.id) ? "is-collapsed" : ""}`, children: [_jsx(NavigationItem, { active: project.id === activeProjectId, icon: _jsx(Folder, { size: 17 }), label: project.name, onCreateChat: () => onProjectChatCreate(project.id), onDelete: () => onProjectDelete(project.id), onOpen: () => {
                                        setCollapsedProjectIds((current) => current.includes(project.id)
                                            ? current.filter((id) => id !== project.id)
                                            : [...current, project.id]);
                                        onProjectChange(project.id);
                                    }, onPin: () => onProjectPin(project.id), pinned: pinnedProjectIds.includes(project.id), ...editProps("project", project.id, project.name) }), !collapsedProjectIds.includes(project.id) ? (_jsx("div", { className: "project-chat-list", children: chats.filter((chat) => chat.projectId === project.id).map((chat) => renderChat(chat, true)) })) : null] }, project.id))) }), projects.length > 5 ? (_jsx("button", { className: "show-more", onClick: () => setShowAllProjects((value) => !value), type: "button", children: showAllProjects ? "Скрыть" : "Показать еще" })) : null] }), _jsxs("section", { className: "nav-section chats-section", children: [_jsx("div", { className: "nav-section-title", children: "\u0427\u0430\u0442\u044B" }), _jsx("div", { className: "sidebar-chat-list", children: freeChats.length ? freeChats.map((chat) => renderChat(chat)) : _jsx("div", { className: "sidebar-empty", children: "\u0421\u0432\u043E\u0431\u043E\u0434\u043D\u044B\u0445 \u0447\u0430\u0442\u043E\u0432 \u043F\u043E\u043A\u0430 \u043D\u0435\u0442." }) })] })] }));
}
function NavigationItem({ active = false, chat = false, detail, icon, label, onCreateChat, onDelete, onEditCancel, onEditChange, onEditSave, onMove, onOpen, onPin, onRename, pinned = false, editing = false, editValue = "" }) {
    const [menuAnchor, setMenuAnchor] = useState(null);
    const hasActionMenu = Boolean(onCreateChat || onMove || onPin || onRename || onDelete);
    const openActionMenu = (element) => {
        const rect = element.getBoundingClientRect();
        setMenuAnchor({ top: rect.top, left: rect.right + 8 });
    };
    const closeActionMenu = (relatedTarget) => {
        if (relatedTarget instanceof Element && relatedTarget.closest(".nav-hover-menu"))
            return;
        setMenuAnchor(null);
    };
    const actionMenu = !editing && hasActionMenu && menuAnchor && typeof document !== "undefined"
        ? createPortal(_jsxs("div", { className: "nav-hover-menu", onMouseLeave: () => setMenuAnchor(null), role: "menu", style: { left: menuAnchor.left, top: menuAnchor.top }, children: [_jsxs("button", { className: "nav-menu-action", onClick: onOpen, type: "button", children: [_jsx(Settings2, { size: 14 }), _jsx("span", { children: "\u0420\u0435\u0434\u0430\u043A\u0442\u0438\u0440\u043E\u0432\u0430\u0442\u044C" })] }), onCreateChat ? _jsxs("button", { className: "nav-menu-action", onClick: onCreateChat, type: "button", children: [_jsx(Plus, { size: 14 }), _jsx("span", { children: "\u041D\u043E\u0432\u044B\u0439 \u0447\u0430\u0442" })] }) : null, onMove ? _jsxs("button", { className: "nav-menu-action", onClick: onMove, type: "button", children: [_jsx(FolderInput, { size: 14 }), _jsx("span", { children: "\u041F\u0435\u0440\u0435\u043D\u0435\u0441\u0442\u0438 \u0432 \u043F\u0440\u043E\u0435\u043A\u0442" })] }) : null, onPin ? _jsxs("button", { className: "nav-menu-action", onClick: onPin, type: "button", children: [_jsx(Pin, { size: 14 }), _jsx("span", { children: pinned ? "Открепить" : "Закрепить" })] }) : null, onRename ? _jsxs("button", { className: "nav-menu-action", onClick: onRename, type: "button", children: [_jsx(Pencil, { size: 14 }), _jsx("span", { children: "\u041F\u0435\u0440\u0435\u0438\u043C\u0435\u043D\u043E\u0432\u0430\u0442\u044C" })] }) : null, onDelete ? _jsxs("button", { className: "nav-menu-action danger", onClick: onDelete, type: "button", children: [_jsx(Trash2, { size: 14 }), _jsx("span", { children: "\u0423\u0434\u0430\u043B\u0438\u0442\u044C" })] }) : null] }), document.body)
        : null;
    return (_jsxs("div", { className: "nav-row", onMouseEnter: (event) => hasActionMenu && openActionMenu(event.currentTarget), onMouseLeave: (event) => closeActionMenu(event.relatedTarget), children: [editing ? (_jsxs("div", { className: `nav-item nav-item-edit ${chat ? "chat-nav-item" : ""} ${active ? "active" : ""}`, children: [icon, _jsx("input", { "aria-label": "\u041D\u043E\u0432\u043E\u0435 \u043D\u0430\u0437\u0432\u0430\u043D\u0438\u0435", autoFocus: true, onChange: (event) => onEditChange?.(event.target.value), onKeyDown: (event) => {
                            if (event.key === "Enter")
                                onEditSave?.();
                            if (event.key === "Escape")
                                onEditCancel?.();
                        }, value: editValue }), _jsx("button", { className: "nav-inline-action", onClick: onEditSave, title: "\u0421\u043E\u0445\u0440\u0430\u043D\u0438\u0442\u044C", type: "button", children: _jsx(CheckCircle2, { size: 14 }) }), _jsx("button", { className: "nav-inline-action", onClick: onEditCancel, title: "\u041E\u0442\u043C\u0435\u043D\u0438\u0442\u044C", type: "button", children: _jsx(X, { size: 14 }) })] })) : (_jsxs("button", { className: `nav-item ${chat ? "chat-nav-item" : ""} ${active ? "active" : ""}`, onClick: onOpen, type: "button", children: [icon, detail ? _jsxs("span", { className: "nav-item-copy", children: [_jsx("span", { children: label }), _jsx("small", { children: detail })] }) : _jsx("span", { children: label })] })), actionMenu] }));
}
function FlowModeSelector({ isRunning }) {
    const [mode, setMode] = useState("python");
    const [status, setStatus] = useState("");
    useEffect(() => {
        fetch("/api/flow-mode", { cache: "no-store" })
            .then(async (response) => {
            const data = (await response.json());
            if (!response.ok)
                throw new Error(data.error || response.statusText);
            if (data.mode)
                setMode(data.mode);
        })
            .catch((error) => setStatus(error instanceof Error ? error.message : "Не удалось загрузить режим flow"));
    }, []);
    const change = async (next) => {
        if (next === mode || isRunning)
            return;
        setStatus("");
        try {
            const data = await postJson("/api/flow-mode", { mode: next }, DEBUG_TIMEOUT_MS);
            setMode(data.mode);
            setStatus(data.cache_cleared ? "Режим переключён, кэш ответов очищен." : "Выбран для следующих запросов.");
        }
        catch (error) {
            setStatus(error instanceof Error ? error.message : "Не удалось переключить flow");
        }
    };
    return (_jsxs("section", { className: "flow-mode-selector", "aria-label": "\u0412\u044B\u0431\u043E\u0440 flow", children: [_jsxs("div", { className: "loader-title", children: [_jsx(Cpu, { size: 15 }), _jsx("span", { children: "\u0420\u0435\u0436\u0438\u043C \u043E\u0431\u0440\u0430\u0431\u043E\u0442\u043A\u0438" })] }), _jsxs("select", { disabled: isRunning, value: mode, onChange: (event) => void change(event.target.value), children: [_jsx("option", { value: "python", children: "1. \u0418\u0441\u043F\u043E\u043B\u044C\u0437\u043E\u0432\u0430\u0442\u044C Python" }), _jsx("option", { value: "rust", children: "2. \u0418\u0441\u043F\u043E\u043B\u044C\u0437\u043E\u0432\u0430\u0442\u044C Rust" }), _jsx("option", { value: "hybrid", children: "3. \u0413\u0438\u0431\u0440\u0438\u0434: Rust + Python" })] }), _jsx("div", { className: "prompt-description", children: mode === "hybrid" ? "Rust ищет контекст, Python формирует и проверяет ответ." : mode === "rust" ? "Полный Rust flow: retrieval и генерация." : "Текущий проверенный Python flow." }), status ? _jsx("div", { className: "provider-status", children: status }) : null] }));
}
function SystemPromptSelector({ isRunning }) {
    const [prompts, setPrompts] = useState([]);
    const [selected, setSelected] = useState("rag-grounded");
    const [status, setStatus] = useState("");
    const load = useCallback(async () => {
        try {
            const response = await fetch("/api/system-prompts", { cache: "no-store" });
            const data = (await response.json());
            if (!response.ok)
                throw new Error(data.error || response.statusText);
            setPrompts(data.prompts);
            setSelected(data.selected);
        }
        catch (error) {
            setStatus(error instanceof Error ? error.message : "Не удалось загрузить системные промты");
        }
    }, []);
    useEffect(() => { void load(); }, [load]);
    const change = async (promptId) => {
        if (promptId === selected || isRunning)
            return;
        setStatus("");
        try {
            const data = await postJson("/api/system-prompts/select", { prompt_id: promptId }, DEBUG_TIMEOUT_MS);
            setSelected(data.selected);
            setStatus("Выбран для следующих запросов.");
        }
        catch (error) {
            setStatus(error instanceof Error ? error.message : "Не удалось переключить промт");
        }
    };
    const active = prompts.find((prompt) => prompt.id === selected);
    return (_jsxs("section", { className: "system-prompt-selector", "aria-label": "\u0412\u044B\u0431\u043E\u0440 \u0441\u0438\u0441\u0442\u0435\u043C\u043D\u043E\u0433\u043E \u043F\u0440\u043E\u043C\u0442\u0430", children: [_jsxs("div", { className: "loader-title", children: [_jsx(Sparkles, { size: 15 }), _jsx("span", { children: "\u0421\u0438\u0441\u0442\u0435\u043C\u043D\u044B\u0439 \u043F\u0440\u043E\u043C\u0442" })] }), _jsx("select", { disabled: isRunning || !prompts.length, value: selected, onChange: (event) => void change(event.target.value), children: prompts.map((prompt) => _jsx("option", { value: prompt.id, children: prompt.name }, prompt.id)) }), active ? _jsx("div", { className: "prompt-description", children: active.description }) : null, status ? _jsx("div", { className: "provider-status", children: status }) : null] }));
}
function ProviderSettings({ models, onProviderChange }) {
    const [config, setConfig] = useState({ provider: "ollama", model: "" });
    const [defaults, setDefaults] = useState({});
    const [apiKey, setApiKey] = useState("");
    const [status, setStatus] = useState("");
    const [isSaving, setIsSaving] = useState(false);
    const load = useCallback(async () => {
        try {
            const response = await fetch("/api/providers", { cache: "no-store" });
            const data = (await response.json());
            if (!response.ok)
                throw new Error(data.error || response.statusText);
            setConfig(data.config);
            setDefaults(data.defaults);
            onProviderChange(data.config.provider);
        }
        catch (error) {
            setStatus(error instanceof Error ? error.message : "Не удалось загрузить настройки");
        }
    }, [onProviderChange]);
    useEffect(() => { void load(); }, [load]);
    const changeProvider = (provider) => {
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
            const result = await postJson(path, payload(), 180000);
            if (result.config) {
                setConfig(result.config);
                setApiKey("");
                onProviderChange(result.config.provider);
                setStatus("Сохранено. Ответы будут генерироваться выбранным провайдером.");
            }
            else {
                setStatus(result.message ?? "Подключение успешно.");
            }
        }
        catch (error) {
            setStatus(error instanceof Error ? error.message : "Ошибка подключения");
        }
        finally {
            setIsSaving(false);
        }
    };
    const isYandex = config.provider === "yandex";
    const needsKey = config.provider !== "ollama";
    return (_jsxs("details", { className: "provider-settings", children: [_jsx("summary", { children: "\u041F\u043E\u0434\u043A\u043B\u044E\u0447\u0435\u043D\u0438\u0435 LLM" }), _jsxs("div", { className: "provider-fields", children: [_jsxs("label", { children: ["\u041F\u0440\u043E\u0432\u0430\u0439\u0434\u0435\u0440", _jsxs("select", { value: config.provider, onChange: (event) => changeProvider(event.target.value), children: [_jsx("option", { value: "ollama", children: "Ollama (\u043B\u043E\u043A\u0430\u043B\u044C\u043D\u043E)" }), _jsx("option", { value: "openai", children: "ChatGPT / OpenAI" }), _jsx("option", { value: "deepseek", children: "DeepSeek" }), _jsx("option", { value: "gemini", children: "Google Gemini" }), _jsx("option", { value: "gigachat", children: "GigaChat" }), _jsx("option", { value: "yandex", children: "YandexGPT" })] })] }), _jsxs("label", { children: ["\u041C\u043E\u0434\u0435\u043B\u044C", config.provider === "ollama" ? (_jsxs("select", { value: config.model, onChange: (event) => setConfig({ ...config, model: event.target.value }), children: [!models.length ? _jsx("option", { value: "", children: "\u041D\u0435\u0442 \u0434\u043E\u0441\u0442\u0443\u043F\u043D\u044B\u0445 \u043C\u043E\u0434\u0435\u043B\u0435\u0439" }) : null, models.map((model) => _jsx("option", { value: model, children: model }, model))] })) : (_jsx("input", { value: config.model, onChange: (event) => setConfig({ ...config, model: event.target.value }) }))] }), config.provider !== "ollama" ? _jsxs("label", { children: ["Base URL", _jsx("input", { value: config.base_url ?? "", onChange: (event) => setConfig({ ...config, base_url: event.target.value }) })] }) : null, isYandex ? _jsxs("label", { children: ["Folder ID", _jsx("input", { value: config.folder_id ?? "", onChange: (event) => setConfig({ ...config, folder_id: event.target.value }) })] }) : null, isYandex ? _jsxs("label", { children: ["\u0422\u0438\u043F \u043A\u043B\u044E\u0447\u0430", _jsxs("select", { value: config.auth_type ?? "api_key", onChange: (event) => setConfig({ ...config, auth_type: event.target.value }), children: [_jsx("option", { value: "api_key", children: "API key" }), _jsx("option", { value: "iam", children: "IAM token" })] })] }) : null, needsKey ? _jsxs("label", { children: ["API-\u043A\u043B\u044E\u0447", _jsx("input", { placeholder: config.api_key_hint || "Введите ключ", type: "password", value: apiKey, onChange: (event) => setApiKey(event.target.value) })] }) : null, _jsxs("div", { className: "provider-actions", children: [_jsxs("button", { disabled: isSaving, onClick: () => void save(true), type: "button", children: [_jsx(TestTube2, { size: 14 }), " \u041F\u0440\u043E\u0432\u0435\u0440\u0438\u0442\u044C"] }), _jsxs("button", { className: "provider-save", disabled: isSaving, onClick: () => void save(false), type: "button", children: [_jsx(Save, { size: 14 }), " \u0421\u043E\u0445\u0440\u0430\u043D\u0438\u0442\u044C"] })] }), status ? _jsx("div", { className: "provider-status", children: status }) : null] })] }));
}
function ModelSelector({ error, isChanging, isLoading, isRunning, models, selectedModel, onChange, onRefresh }) {
    return (_jsxs("section", { className: "model-selector", "aria-label": "\u0412\u044B\u0431\u043E\u0440 LLM", children: [_jsxs("div", { className: "loader-title", children: [_jsx(Cpu, { size: 15 }), _jsx("span", { children: "\u041C\u043E\u0434\u0435\u043B\u044C LLM" }), _jsx("button", { className: "refresh-models", disabled: isLoading || isChanging, onClick: onRefresh, title: "\u041E\u0431\u043D\u043E\u0432\u0438\u0442\u044C \u0441\u043F\u0438\u0441\u043E\u043A", type: "button", children: _jsx(RefreshCw, { size: 14, className: isLoading ? "spin" : "" }) })] }), _jsxs("select", { "aria-label": "\u0412\u044B\u0431\u0440\u0430\u043D\u043D\u0430\u044F \u043C\u043E\u0434\u0435\u043B\u044C", className: "model-select", disabled: isLoading || isChanging || isRunning || !models.length, onChange: (event) => onChange(event.target.value), value: selectedModel, children: [!models.length ? _jsx("option", { value: "", children: isLoading ? "Загрузка моделей..." : "Модели не найдены" }) : null, selectedModel && !models.includes(selectedModel) ? _jsxs("option", { value: selectedModel, children: [selectedModel, " (\u043D\u0435 \u043D\u0430\u0439\u0434\u0435\u043D\u0430)"] }) : null, models.map((model) => _jsx("option", { value: model, children: model }, model))] }), isChanging ? _jsx("div", { className: "loader-muted", children: "\u041F\u0435\u0440\u0435\u043A\u043B\u044E\u0447\u0435\u043D\u0438\u0435 \u043C\u043E\u0434\u0435\u043B\u0438\u2026" }) : null, error ? _jsx("div", { className: "model-error", children: error }) : null] }));
}
function KnowledgeLoader({ error, isUploading, result, selectedFiles, onFilesChange, onUpload }) {
    const inputId = "knowledge-file-input";
    return (_jsxs("section", { className: "knowledge-loader", "aria-label": "\u0417\u0430\u0433\u0440\u0443\u0437\u043A\u0430 \u0432 \u0431\u0430\u0437\u0443 \u0437\u043D\u0430\u043D\u0438\u0439", children: [_jsxs("div", { className: "loader-title", children: [_jsx(Database, { size: 15 }), _jsx("span", { children: "\u0411\u0430\u0437\u0430 \u0437\u043D\u0430\u043D\u0438\u0439" })] }), _jsxs("label", { className: "file-picker", htmlFor: inputId, children: [_jsx(UploadCloud, { size: 17 }), _jsx("span", { children: selectedFiles.length ? `Выбрано: ${selectedFiles.length}` : "Выбрать файлы" })] }), _jsx("input", { accept: ".docx,.pdf,.md,.txt", className: "file-input", id: inputId, multiple: true, onChange: (event) => onFilesChange(Array.from(event.currentTarget.files ?? [])), type: "file" }), selectedFiles.length ? (_jsxs("div", { className: "selected-files", children: [selectedFiles.slice(0, 4).map((file) => (_jsxs("div", { className: "selected-file", children: [_jsx(FileText, { size: 13 }), _jsx("span", { children: file.name })] }, `${file.name}-${file.size}`))), selectedFiles.length > 4 ? _jsxs("div", { className: "loader-muted", children: ["\u0438 \u0435\u0449\u0435 ", selectedFiles.length - 4] }) : null] })) : null, _jsxs("button", { className: "upload-button", disabled: !selectedFiles.length || isUploading, onClick: onUpload, type: "button", children: [_jsx(UploadCloud, { size: 15 }), isUploading ? "Загрузка..." : "Загрузить"] }), result ? (_jsxs("div", { className: "upload-status success", children: [_jsx(CheckCircle2, { size: 14 }), _jsxs("span", { children: [result.files.length, " \u0444\u0430\u0439\u043B(\u043E\u0432), ", result.chunks, " \u0447\u0430\u043D\u043A\u043E\u0432"] })] })) : null, error ? (_jsxs("div", { className: "upload-status error", children: [_jsx(AlertTriangle, { size: 14 }), _jsx("span", { children: error })] })) : null] }));
}
function Thread() {
    return (_jsxs(ThreadPrimitive.Root, { className: "thread-root", children: [_jsxs(ThreadPrimitive.Viewport, { className: "thread-viewport", children: [_jsx(ThreadPrimitive.Empty, { children: _jsxs("div", { className: "empty-state", children: [_jsx("div", { className: "empty-icon", children: _jsx(Bot, { size: 24 }) }), _jsx("h1", { children: "\u0417\u0430\u0434\u0430\u0439 \u0432\u043E\u043F\u0440\u043E\u0441 \u043F\u043E \u0440\u0435\u0433\u043B\u0430\u043C\u0435\u043D\u0442\u0430\u043C" }), _jsx("p", { children: "\u041C\u043E\u0436\u043D\u043E \u0441\u043F\u0440\u0430\u0448\u0438\u0432\u0430\u0442\u044C \u043F\u043E \u043D\u043E\u043C\u0435\u0440\u0443 \u043F\u0443\u043D\u043A\u0442\u0430, \u043D\u0430\u0437\u0432\u0430\u043D\u0438\u044E \u0440\u0430\u0437\u0434\u0435\u043B\u0430 \u0438\u043B\u0438 \u0442\u0435\u0440\u043C\u0438\u043D\u0430\u043C \u0432\u0440\u043E\u0434\u0435 \u0418\u04120, \u0418\u04121, \u0418\u0421." })] }) }), _jsx(ThreadPrimitive.Messages, { components: { Message } })] }), _jsx(ThreadPrimitive.ViewportFooter, { className: "thread-footer", children: _jsx(Composer, {}) })] }));
}
function Message() {
    const role = useMessage((state) => state.role);
    const html = useMessage((state) => state.metadata.custom.html);
    const modelLabel = useMessage((state) => state.metadata.custom.modelLabel);
    const isAssistant = role === "assistant";
    return (_jsxs(MessagePrimitive.Root, { className: `message-row ${isAssistant ? "assistant" : "user"}`, children: [_jsx("div", { className: "message-avatar", children: isAssistant ? _jsx(Bot, { size: 16 }) : _jsx(User, { size: 16 }) }), _jsxs("div", { className: "message-body", children: [isAssistant && typeof modelLabel === "string" && modelLabel ? (_jsxs("div", { className: "model-badge", children: ["\u041E\u0442\u0432\u0435\u0442\u0438\u043B: ", modelLabel] })) : null, isAssistant && typeof html === "string" && html ? (_jsx(HtmlBlock, { className: "answer-html", html: html })) : (_jsx(MessagePrimitive.Content, {})), _jsx(ActionBarPrimitive.Root, { className: "message-actions", children: _jsx(ActionBarPrimitive.Copy, { className: "icon-button", title: "\u041A\u043E\u043F\u0438\u0440\u043E\u0432\u0430\u0442\u044C", "aria-label": "\u041A\u043E\u043F\u0438\u0440\u043E\u0432\u0430\u0442\u044C", children: _jsx(Copy, { size: 14 }) }) })] })] }));
}
function HtmlBlock({ className, html }) {
    const ref = useMathTypeset(html);
    return (_jsx("div", { className: className, dangerouslySetInnerHTML: { __html: html }, ref: ref }));
}
function Composer() {
    return (_jsxs(ComposerPrimitive.Root, { className: "composer", children: [_jsx(ComposerPrimitive.Input, { className: "composer-input", placeholder: "\u041D\u0430\u043F\u0440\u0438\u043C\u0435\u0440: 2.1.4 \u0423\u0447\u0435\u0442 \u043E\u043F\u0435\u0440\u0430\u0442\u0438\u0432\u043D\u044B\u0445 \u0446\u0435\u043D\u043E\u043F\u0440\u0438\u043D\u0438\u043C\u0430\u044E\u0449\u0438\u0445 \u0437\u0430\u044F\u0432\u043E\u043A", submitMode: "enter" }), _jsx(ComposerPrimitive.Send, { className: "send-button", title: "\u041E\u0442\u043F\u0440\u0430\u0432\u0438\u0442\u044C", "aria-label": "\u041E\u0442\u043F\u0440\u0430\u0432\u0438\u0442\u044C", children: _jsx(Send, { size: 18 }) })] }));
}
function EvidencePanel({ activeTab, activeProject, activeProvider, theme, debugError, debugResult, isDebugging, isChangingModel, isLoadingModels, isRunning, lastQuestion, modelError, models, result, selectedModel, onChangeModel, onDebug, onProviderSettingsChange, onProjectMemoryChange, onRefreshModels, onTabChange, onThemeChange, error, isUploading, selectedFiles, uploadResult, onFilesChange, onUpload }) {
    const [isThemeModalOpen, setIsThemeModalOpen] = useState(false);
    return (_jsxs("aside", { className: "evidence-panel", children: [_jsxs("div", { className: "workspace-heading", children: [_jsxs("div", { className: "panel-heading", children: [_jsx(PanelRight, { size: 17 }), _jsx("span", { children: "\u0422\u0435\u0445\u043D\u0438\u0447\u0435\u0441\u043A\u0430\u044F \u043F\u0430\u043D\u0435\u043B\u044C" })] }), _jsx("button", { className: "icon-button panel-settings", onClick: () => setIsThemeModalOpen(true), title: "\u041D\u0430\u0441\u0442\u0440\u043E\u0439\u043A\u0438 \u0440\u0430\u0431\u043E\u0447\u0435\u0439 \u043E\u0431\u043B\u0430\u0441\u0442\u0438", type: "button", children: _jsx(Settings2, { size: 16 }) })] }), _jsxs("div", { className: "panel-tabs", role: "tablist", "aria-label": "Evidence panels", children: [_jsxs("button", { className: activeTab === "system" ? "active" : "", onClick: () => onTabChange("system"), type: "button", children: [_jsx(Settings2, { size: 14 }), "\u0421\u0438\u0441\u0442\u0435\u043C\u0430"] }), _jsxs("button", { className: activeTab === "evidence" ? "active" : "", onClick: () => onTabChange("evidence"), type: "button", children: [_jsx(Sigma, { size: 14 }), "Evidence"] }), _jsxs("button", { className: activeTab === "sources" ? "active" : "", onClick: () => onTabChange("sources"), type: "button", children: [_jsx(Files, { size: 14 }), "Sources"] }), _jsxs("button", { className: activeTab === "debug" ? "active" : "", onClick: () => onTabChange("debug"), type: "button", children: [_jsx(Bug, { size: 14 }), "Debug"] })] }), activeTab === "system" ? (_jsxs("div", { className: "system-panel", children: [_jsxs("div", { className: "system-intro", children: [_jsx("div", { className: "system-kicker", children: "\u0420\u0430\u0431\u043E\u0447\u0438\u0435 \u043D\u0430\u0441\u0442\u0440\u043E\u0439\u043A\u0438" }), _jsx("h2", { children: "\u0421\u0438\u0441\u0442\u0435\u043C\u0430" }), _jsx("p", { children: "\u041F\u0430\u0440\u0430\u043C\u0435\u0442\u0440\u044B \u043D\u0438\u0436\u0435 \u043F\u0440\u0438\u043C\u0435\u043D\u044F\u044E\u0442\u0441\u044F \u043A \u0441\u043B\u0435\u0434\u0443\u044E\u0449\u0438\u043C \u0437\u0430\u043F\u0440\u043E\u0441\u0430\u043C." })] }), _jsxs("section", { className: "project-memory-panel", children: [_jsxs("div", { className: "project-memory-heading", children: [_jsx(Folder, { size: 14 }), _jsx("span", { children: activeProject.name })] }), _jsxs("label", { children: [_jsx("span", { children: "\u041F\u0430\u043C\u044F\u0442\u044C \u043F\u0440\u043E\u0435\u043A\u0442\u0430" }), _jsx("textarea", { value: activeProject.memory, onChange: (event) => onProjectMemoryChange(event.target.value) })] })] }), _jsx(ProviderSettings, { models: models, onProviderChange: onProviderSettingsChange }), _jsx(FlowModeSelector, { isRunning: isRunning }), _jsx(SystemPromptSelector, { isRunning: isRunning }), activeProvider === "ollama" ? (_jsx(ModelSelector, { error: modelError, isChanging: isChangingModel, isLoading: isLoadingModels, isRunning: isRunning, models: models, selectedModel: selectedModel, onChange: onChangeModel, onRefresh: onRefreshModels })) : null, _jsx(KnowledgeLoader, { error: error, isUploading: isUploading, result: uploadResult, selectedFiles: selectedFiles, onFilesChange: onFilesChange, onUpload: onUpload })] })) : !result ? (_jsx("div", { className: "panel-empty", children: "\u0417\u0434\u0435\u0441\u044C \u043F\u043E\u044F\u0432\u044F\u0442\u0441\u044F \u0438\u0441\u0442\u043E\u0447\u043D\u0438\u043A\u0438 \u043F\u043E\u0441\u043B\u0435\u0434\u043D\u0435\u0433\u043E \u043E\u0442\u0432\u0435\u0442\u0430." })) : activeTab === "evidence" ? (_jsx(EvidenceSummary, { result: result })) : activeTab === "sources" ? (_jsx(SourcesPanel, { result: result })) : (_jsx(DebugPanel, { debugError: debugError, debugResult: debugResult, fallbackContext: result.context, isDebugging: isDebugging, lastQuestion: lastQuestion, onDebug: onDebug })), isThemeModalOpen ? (_jsx("div", { className: "theme-modal-backdrop", role: "presentation", onMouseDown: () => setIsThemeModalOpen(false), children: _jsxs("section", { className: "theme-modal", role: "dialog", "aria-modal": "true", "aria-labelledby": "theme-modal-title", onMouseDown: (event) => event.stopPropagation(), children: [_jsxs("div", { className: "theme-modal-heading", children: [_jsxs("div", { children: [_jsx("div", { className: "system-kicker", children: "\u041D\u0430\u0441\u0442\u0440\u043E\u0439\u043A\u0438 \u0438\u043D\u0442\u0435\u0440\u0444\u0435\u0439\u0441\u0430" }), _jsx("h2", { id: "theme-modal-title", children: "\u0422\u0435\u043C\u0430" })] }), _jsx("button", { className: "icon-button", onClick: () => setIsThemeModalOpen(false), title: "\u0417\u0430\u043A\u0440\u044B\u0442\u044C", type: "button", children: _jsx(X, { size: 17 }) })] }), _jsx(ThemeSelector, { theme: theme, onChange: onThemeChange })] }) })) : null] }));
}
function ThemeSelector({ theme, onChange }) {
    return (_jsxs("section", { className: "theme-selector", "aria-label": "\u0422\u0435\u043C\u0430 \u0438\u043D\u0442\u0435\u0440\u0444\u0435\u0439\u0441\u0430", children: [_jsxs("div", { className: "theme-selector-title", children: [_jsx(Paintbrush, { size: 14 }), " \u0422\u0435\u043C\u0430 \u0438\u043D\u0442\u0435\u0440\u0444\u0435\u0439\u0441\u0430"] }), _jsxs("select", { value: theme, onChange: (event) => onChange(event.target.value), children: [_jsx("option", { value: "default", children: "\u0411\u0430\u0437\u043E\u0432\u0430\u044F" }), _jsx("option", { value: "portal", children: "\u041A\u043E\u0440\u043F\u043E\u0440\u0430\u0442\u0438\u0432\u043D\u0430\u044F" })] }), _jsx("div", { className: "theme-selector-description", children: theme === "portal" ? "Контрастная палитра для встраивания в корпоративный портал." : "Нейтральная тема RAG Assistant." })] }));
}
function EvidenceSummary({ result }) {
    return (_jsxs(_Fragment, { children: [_jsxs("div", { className: "metrics", children: [_jsx("span", { children: result.from_cache ? "cache" : `${result.elapsed_s.toFixed(1)}с` }), _jsxs("span", { children: ["total ~", result.usage?.total_tokens_est ?? 0] }), _jsxs("span", { children: ["answer ~", result.usage?.answer_tokens_est ?? 0] }), _jsxs("span", { children: ["context ~", result.usage?.context_tokens_est ?? 0] })] }), _jsx(PanelSection, { title: "\u0420\u0430\u0441\u0447\u0435\u0442\u043D\u044B\u0435 \u043F\u0440\u0430\u0432\u0438\u043B\u0430", children: result.formulas?.length ? (result.formulas.slice(0, 8).map((formula, index) => (_jsx(HtmlCard, { className: "rule", fallback: formula.text, html: formula.html }, `${formula.text}-${index}`)))) : (_jsx("div", { className: "muted", children: "\u041D\u0435 \u0432\u044B\u0434\u0435\u043B\u0435\u043D\u044B." })) }), _jsx(PanelSection, { title: "\u041F\u0440\u043E\u0432\u0435\u0440\u043A\u0430", children: result.warnings?.length ? (result.warnings.map((warning) => (_jsxs("div", { className: "warning", children: [_jsx(AlertTriangle, { size: 14 }), _jsx("span", { children: warning })] }, warning)))) : (_jsx("div", { className: "muted", children: "\u041A\u0440\u0438\u0442\u0438\u0447\u043D\u044B\u0445 \u043F\u0440\u0435\u0434\u0443\u043F\u0440\u0435\u0436\u0434\u0435\u043D\u0438\u0439 \u043D\u0435\u0442." })) })] }));
}
function SourcesPanel({ result }) {
    return (_jsx(PanelSection, { title: "\u0418\u0441\u0442\u043E\u0447\u043D\u0438\u043A\u0438 \u043E\u0442\u0432\u0435\u0442\u0430", children: result.sources?.length ? (result.sources.map((source, index) => (_jsxs("div", { className: "source-item", children: [_jsxs("div", { className: "source-file", children: [_jsx(FileText, { size: 14 }), _jsx("span", { children: source.file })] }), _jsxs("div", { className: "source-meta", children: [source.section ? _jsx("span", { children: source.section }) : null, _jsxs("span", { children: ["score=", source.score] })] }), _jsx(HtmlCard, { className: "source-preview", fallback: source.text_preview, html: source.html_preview })] }, `${source.file}-${index}`)))) : (_jsx("div", { className: "muted", children: "\u041D\u0435\u0442 \u0438\u0441\u0442\u043E\u0447\u043D\u0438\u043A\u043E\u0432." })) }));
}
function DebugPanel({ debugError, debugResult, fallbackContext, isDebugging, lastQuestion, onDebug }) {
    const context = debugResult?.context ?? fallbackContext ?? [];
    return (_jsxs(_Fragment, { children: [_jsxs("button", { className: "debug-button", disabled: !lastQuestion || isDebugging, onClick: onDebug, type: "button", children: [_jsx(Bug, { size: 15 }), isDebugging ? "Debug..." : "Debug context"] }), debugError ? _jsx("div", { className: "warning", children: debugError }) : null, _jsx(PanelSection, { title: "\u041D\u0430\u0439\u0434\u0435\u043D\u043D\u044B\u0439 \u043A\u043E\u043D\u0442\u0435\u043A\u0441\u0442", children: context.length ? (context.map((chunk, index) => (_jsxs("div", { className: "context-item", children: [_jsxs("div", { className: "source-file", children: [_jsx(FileText, { size: 14 }), _jsxs("span", { children: [index + 1, ". ", chunk.file] })] }), _jsxs("div", { className: "source-meta", children: [chunk.flow_origin ? _jsxs("span", { children: ["retrieval: ", chunk.flow_origin] }) : null, chunk.section_path ? _jsx("span", { children: chunk.section_path }) : null, chunk.chunk_type ? _jsxs("span", { children: ["type: ", chunk.chunk_type] }) : null, chunk.status && chunk.status !== "active" ? _jsxs("span", { children: ["status: ", chunk.status] }) : null, _jsxs("span", { children: ["score=", chunk.score] })] }), _jsx(HtmlCard, { className: "context-text", fallback: chunk.text, html: chunk.html_text })] }, `${chunk.file}-${index}`)))) : (_jsx("div", { className: "muted", children: "\u041D\u0430\u0436\u043C\u0438 Debug context, \u0447\u0442\u043E\u0431\u044B \u043F\u043E\u043B\u0443\u0447\u0438\u0442\u044C \u0444\u0440\u0430\u0433\u043C\u0435\u043D\u0442\u044B \u0431\u0435\u0437 \u0432\u044B\u0437\u043E\u0432\u0430 \u043C\u043E\u0434\u0435\u043B\u0438." })) })] }));
}
function HtmlCard({ className, fallback, html }) {
    if (html)
        return _jsx(HtmlBlock, { className: className, html: html });
    return _jsx("div", { className: className, children: fallback || "Нет данных." });
}
function PanelSection({ title, children }) {
    return (_jsxs("section", { className: "panel-section", children: [_jsx("div", { className: "panel-section-title", children: title }), children] }));
}
