type ApiErrorPayload = {
  error?: string;
};

function isTimedOut(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

async function post<T>(
  path: string,
  options: RequestInit,
  timeoutMs: number,
  timeoutMessage: string
): Promise<T> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(path, { ...options, signal: controller.signal });
    const data = (await response.json()) as T & ApiErrorPayload;
    if (!response.ok) {
      throw new Error(data.error || response.statusText);
    }
    return data;
  } catch (error) {
    if (isTimedOut(error)) {
      throw new Error(timeoutMessage);
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

export function postJson<T>(path: string, body: unknown, timeoutMs: number): Promise<T> {
  return post<T>(
    path,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    },
    timeoutMs,
    "Backend не ответил вовремя. Запрос можно повторить."
  );
}

export function postFormData<T>(path: string, body: FormData, timeoutMs: number): Promise<T> {
  return post<T>(
    path,
    { method: "POST", body },
    timeoutMs,
    "Загрузка не завершилась вовремя. Попробуй меньший файл или повтори позже."
  );
}
