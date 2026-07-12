import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
import { ActionBarPrimitive, AssistantRuntimeProvider, ComposerPrimitive, MessagePrimitive, ThreadPrimitive, useExternalStoreRuntime, useMessage } from "@assistant-ui/react";
import { AlertTriangle, Bot, Bug, CheckCircle2, Copy, Database, Cpu, FileText, Files, PanelRight, RefreshCw, RotateCcw, Save, Send, Sigma, Sparkles, TestTube2, UploadCloud, User } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
const DEBUG_TIMEOUT_MS = 60000;
const UPLOAD_TIMEOUT_MS = 300000;
const createId = () => crypto.randomUUID();
async function postJson(path, body, timeoutMs) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
            credentials: "include",
            signal: controller.signal
        });
        const data = (await response.json());
        if (!response.ok) {
            throw new Error(data.error || response.statusText);
        }
        return data;
    }
    catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
            throw new Error("Backend не ответил вовремя. Запрос можно повторить.");
        }
        throw error;
    }
    finally {
        window.clearTimeout(timeoutId);
    }
}
async function postFormData(path, body, timeoutMs) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(path, {
            method: "POST",
            body,
            credentials: "include",
            signal: controller.signal
        });
        const data = (await response.json());
        if (!response.ok) {
            throw new Error(data.error || response.statusText);
        }
        return data;
    }
    catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
            throw new Error("Загрузка не завершилась вовремя. Попробуй меньший файл или повтори позже.");
        }
        throw error;
    }
    finally {
        window.clearTimeout(timeoutId);
    }
}
async function postSse(path, body, onDelta) {
    const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
        credentials: "include"
    });
    if (!response.ok || !response.body)
        throw new Error(response.statusText || "SSE недоступен");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let completed = null;
    while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const event = block.split("\n").find((line) => line.startsWith("event:"))?.slice(6).trim();
            const dataText = block.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
            const data = dataText ? JSON.parse(dataText) : {};
            if (event === "delta")
                onDelta(String(data.text || ""));
            if (event === "completed")
                completed = data;
            if (event === "error")
                throw new Error(String(data.message || "Ошибка генерации"));
            boundary = buffer.indexOf("\n\n");
        }
        if (done)
            break;
    }
    if (!completed)
        throw new Error("SSE завершился без результата");
    return completed;
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
    const [activeTab, setActiveTab] = useState("evidence");
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
    const loadModels = useCallback(async () => {
        setIsLoadingModels(true);
        setModelError("");
        try {
            const response = await fetch("/api/models", { cache: "no-store", credentials: "include" });
            const data = (await response.json());
            if (!response.ok)
                throw new Error(data.error || response.statusText);
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
    }, [loadModels]);
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
    const onNew = useCallback(async (message) => {
        const question = extractText(message);
        if (!question)
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
        setActiveTab("evidence");
        try {
            let streamedText = "";
            const data = await postSse("/api/ask/stream", { question }, (delta) => {
                streamedText += delta;
                setMessages((current) => current.map((item) => item.id === assistantId ? { ...item, text: streamedText } : item));
            });
            setLastResult(data);
            setMessages((current) => current.map((item) => item.id === assistantId
                ? {
                    ...item,
                    text: data.answer || streamedText || "Пустой ответ.",
                    html: data.html_answer,
                    modelLabel: data.llm?.label,
                    status: "complete"
                }
                : item));
        }
        catch (error) {
            const text = error instanceof Error ? error.message : "Ошибка запроса";
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
    }, []);
    const adapter = useMemo(() => ({
        messages,
        isRunning,
        onNew,
        setMessages: (next) => setMessages([...next]),
        convertMessage: toThreadMessage
    }), [messages, isRunning, onNew]);
    const runtime = useExternalStoreRuntime(adapter);
    const clearThread = () => {
        setMessages([]);
        setLastResult(null);
        setLastQuestion("");
        setDebugResult(null);
        setDebugError("");
    };
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
    return (_jsx(AssistantRuntimeProvider, { runtime: runtime, children: _jsxs("main", { className: "app-shell", children: [_jsxs("aside", { className: "sidebar", children: [_jsxs("div", { className: "brand", children: [_jsx("div", { className: "brand-mark", children: _jsx(Sparkles, { size: 18 }) }), _jsxs("div", { children: [_jsx("div", { className: "brand-title", children: "RAG Assistant" }), _jsx("div", { className: "brand-subtitle", children: "Ollama + Qdrant" })] })] }), _jsxs("button", { className: "new-thread", onClick: clearThread, type: "button", children: [_jsx(RotateCcw, { size: 16 }), "\u041D\u043E\u0432\u044B\u0439 \u0434\u0438\u0430\u043B\u043E\u0433"] }), _jsx(ProviderSettings, { models: models, onProviderChange: setActiveProvider }), _jsx(FlowModeSelector, { isRunning: isRunning }), _jsx(SystemPromptSelector, { isRunning: isRunning }), activeProvider === "ollama" ? (_jsx(ModelSelector, { error: modelError, isChanging: isChangingModel, isLoading: isLoadingModels, isRunning: isRunning, models: models, selectedModel: selectedModel, onChange: changeModel, onRefresh: loadModels })) : null, _jsx(KnowledgeLoader, { error: uploadError, isUploading: isUploading, result: uploadResult, selectedFiles: selectedFiles, onFilesChange: setSelectedFiles, onUpload: uploadFiles }), _jsx("div", { className: "sidebar-note", children: "\u0418\u043D\u0442\u0435\u0440\u0444\u0435\u0439\u0441 \u0440\u0430\u0431\u043E\u0442\u0430\u0435\u0442 \u0447\u0435\u0440\u0435\u0437 assistant-ui \u0438 \u043B\u043E\u043A\u0430\u043B\u044C\u043D\u044B\u0439 backend 127.0.0.1:8080." })] }), _jsx("section", { className: "chat-surface", children: _jsx(Thread, {}) }), _jsx(EvidencePanel, { activeTab: activeTab, debugError: debugError, debugResult: debugResult, isDebugging: isDebugging, lastQuestion: lastQuestion, result: lastResult, onDebug: runDebug, onTabChange: setActiveTab })] }) }));
}
function FlowModeSelector({ isRunning }) {
    const [mode, setMode] = useState("python");
    const [status, setStatus] = useState("");
    useEffect(() => {
        fetch("/api/flow-mode", { cache: "no-store", credentials: "include" })
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
            const response = await fetch("/api/system-prompts", { cache: "no-store", credentials: "include" });
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
            const response = await fetch("/api/providers", { cache: "no-store", credentials: "include" });
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
    return (_jsx(ThreadPrimitive.Root, { className: "thread-root", children: _jsxs(ThreadPrimitive.Viewport, { className: "thread-viewport", children: [_jsx(ThreadPrimitive.Empty, { children: _jsxs("div", { className: "empty-state", children: [_jsx("div", { className: "empty-icon", children: _jsx(Bot, { size: 24 }) }), _jsx("h1", { children: "\u0417\u0430\u0434\u0430\u0439 \u0432\u043E\u043F\u0440\u043E\u0441 \u043F\u043E \u0440\u0435\u0433\u043B\u0430\u043C\u0435\u043D\u0442\u0430\u043C" }), _jsx("p", { children: "\u041C\u043E\u0436\u043D\u043E \u0441\u043F\u0440\u0430\u0448\u0438\u0432\u0430\u0442\u044C \u043F\u043E \u043D\u043E\u043C\u0435\u0440\u0443 \u043F\u0443\u043D\u043A\u0442\u0430, \u043D\u0430\u0437\u0432\u0430\u043D\u0438\u044E \u0440\u0430\u0437\u0434\u0435\u043B\u0430 \u0438\u043B\u0438 \u0442\u0435\u0440\u043C\u0438\u043D\u0430\u043C \u0432\u0440\u043E\u0434\u0435 \u0418\u04120, \u0418\u04121, \u0418\u0421." })] }) }), _jsx(ThreadPrimitive.Messages, { components: { Message } }), _jsx(ThreadPrimitive.ViewportFooter, { className: "thread-footer", children: _jsx(Composer, {}) })] }) }));
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
function EvidencePanel({ activeTab, debugError, debugResult, isDebugging, lastQuestion, result, onDebug, onTabChange }) {
    return (_jsxs("aside", { className: "evidence-panel", children: [_jsxs("div", { className: "panel-heading", children: [_jsx(PanelRight, { size: 17 }), _jsx("span", { children: "\u041F\u0430\u043D\u0435\u043B\u0438" })] }), _jsxs("div", { className: "panel-tabs", role: "tablist", "aria-label": "Evidence panels", children: [_jsxs("button", { className: activeTab === "evidence" ? "active" : "", onClick: () => onTabChange("evidence"), type: "button", children: [_jsx(Sigma, { size: 14 }), "Evidence"] }), _jsxs("button", { className: activeTab === "sources" ? "active" : "", onClick: () => onTabChange("sources"), type: "button", children: [_jsx(Files, { size: 14 }), "Sources"] }), _jsxs("button", { className: activeTab === "debug" ? "active" : "", onClick: () => onTabChange("debug"), type: "button", children: [_jsx(Bug, { size: 14 }), "Debug"] })] }), !result ? (_jsx("div", { className: "panel-empty", children: "\u0417\u0434\u0435\u0441\u044C \u043F\u043E\u044F\u0432\u044F\u0442\u0441\u044F \u0438\u0441\u0442\u043E\u0447\u043D\u0438\u043A\u0438 \u043F\u043E\u0441\u043B\u0435\u0434\u043D\u0435\u0433\u043E \u043E\u0442\u0432\u0435\u0442\u0430." })) : activeTab === "evidence" ? (_jsx(EvidenceSummary, { result: result })) : activeTab === "sources" ? (_jsx(SourcesPanel, { result: result })) : (_jsx(DebugPanel, { debugError: debugError, debugResult: debugResult, fallbackContext: result.context, isDebugging: isDebugging, lastQuestion: lastQuestion, onDebug: onDebug }))] }));
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
