type ApiErrorPayload = {
  error?: string;
  detail?: string;
};

let csrfToken = "";

export function setCsrfToken(value?: string | null): void {
  csrfToken = value?.trim() ?? "";
}

export function clearCsrfToken(): void {
  csrfToken = "";
}

async function readApiResponse<T>(response: Response): Promise<T & ApiErrorPayload> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return (await response.json()) as T & ApiErrorPayload;
  }

  const body = (await response.text()).replace(/\s+/g, " ").trim();
  const details = body && !body.startsWith("<!") ? `: ${body.slice(0, 240)}` : "";
  throw new Error(`Сервер вернул некорректный ответ (${response.status} ${response.statusText})${details}`);
}

function isTimedOut(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

async function request<T>(
  path: string,
  options: RequestInit,
  timeoutMs: number,
  timeoutMessage: string
): Promise<T> {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const headers = new Headers(options.headers);
    const method = (options.method ?? "GET").toUpperCase();
    if (csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)) {
      headers.set("X-CSRF-Token", csrfToken);
    }
    const response = await fetch(path, {
      ...options,
      headers,
      credentials: "same-origin",
      signal: controller.signal
    });
    const data = await readApiResponse<T>(response);
    if (!response.ok) {
      if (response.status === 401 && !path.startsWith("/api/auth/")) {
        window.dispatchEvent(new Event("rag:unauthorized"));
      }
      throw new Error(data.error || data.detail || response.statusText);
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
  return request<T>(
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
  return request<T>(
    path,
    { method: "POST", body },
    timeoutMs,
    "Загрузка не завершилась вовремя. Попробуй меньший файл или повтори позже."
  );
}

export function getJson<T>(path: string, timeoutMs: number): Promise<T> {
  return request<T>(
    path,
    { cache: "no-store" },
    timeoutMs,
    "Backend не ответил вовремя. Запрос можно повторить."
  );
}

export function patchJson<T>(path: string, body: unknown, timeoutMs: number): Promise<T> {
  return request<T>(
    path,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    },
    timeoutMs,
    "Backend не ответил вовремя. Запрос можно повторить."
  );
}

export function deleteJson<T>(path: string, timeoutMs: number): Promise<T> {
  return request<T>(
    path,
    { method: "DELETE" },
    timeoutMs,
    "Backend не ответил вовремя. Запрос можно повторить."
  );
}
