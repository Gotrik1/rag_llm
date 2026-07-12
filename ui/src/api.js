async function readApiResponse(response) {
    const contentType = response.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
        return (await response.json());
    }
    const body = (await response.text()).replace(/\s+/g, " ").trim();
    const details = body && !body.startsWith("<!") ? `: ${body.slice(0, 240)}` : "";
    throw new Error(`Сервер вернул некорректный ответ (${response.status} ${response.statusText})${details}`);
}
function isTimedOut(error) {
    return error instanceof DOMException && error.name === "AbortError";
}
async function request(path, options, timeoutMs, timeoutMessage) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(path, { ...options, signal: controller.signal });
        const data = await readApiResponse(response);
        if (!response.ok) {
            throw new Error(data.error || response.statusText);
        }
        return data;
    }
    catch (error) {
        if (isTimedOut(error)) {
            throw new Error(timeoutMessage);
        }
        throw error;
    }
    finally {
        window.clearTimeout(timeoutId);
    }
}
export function postJson(path, body, timeoutMs) {
    return request(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
    }, timeoutMs, "Backend не ответил вовремя. Запрос можно повторить.");
}
export function postFormData(path, body, timeoutMs) {
    return request(path, { method: "POST", body }, timeoutMs, "Загрузка не завершилась вовремя. Попробуй меньший файл или повтори позже.");
}
export function getJson(path, timeoutMs) {
    return request(path, { cache: "no-store" }, timeoutMs, "Backend не ответил вовремя. Запрос можно повторить.");
}
export function patchJson(path, body, timeoutMs) {
    return request(path, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
    }, timeoutMs, "Backend не ответил вовремя. Запрос можно повторить.");
}
export function deleteJson(path, timeoutMs) {
    return request(path, { method: "DELETE" }, timeoutMs, "Backend не ответил вовремя. Запрос можно повторить.");
}
