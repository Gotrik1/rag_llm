function isTimedOut(error) {
    return error instanceof DOMException && error.name === "AbortError";
}
async function post(path, options, timeoutMs, timeoutMessage) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(path, { ...options, signal: controller.signal });
        const data = (await response.json());
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
    return post(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
    }, timeoutMs, "Backend не ответил вовремя. Запрос можно повторить.");
}
export function postFormData(path, body, timeoutMs) {
    return post(path, { method: "POST", body }, timeoutMs, "Загрузка не завершилась вовремя. Попробуй меньший файл или повтори позже.");
}
