import { afterEach, describe, expect, it, vi } from "vitest";
import { postFormData, postJson } from "./api";

const fetchMock = vi.fn();

vi.stubGlobal("fetch", fetchMock);

function jsonResponse(data: unknown, ok = true, statusText = "OK") {
  return {
    ok,
    statusText,
    json: vi.fn().mockResolvedValue(data)
  };
}

function pendingUntilAbort() {
  return vi.fn((_path: string, options?: RequestInit) => new Promise((_resolve, reject) => {
    options?.signal?.addEventListener("abort", () => {
      reject(new DOMException("Request aborted", "AbortError"));
    });
  }));
}

afterEach(() => {
  fetchMock.mockReset();
  vi.useRealTimers();
});

describe("HTTP client", () => {
  it("returns parsed JSON for a successful request", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ answer: "Готово" }));

    await expect(postJson<{ answer: string }>("/api/ask", { question: "Тест" }, 1_000))
      .resolves.toEqual({ answer: "Готово" });

    expect(fetchMock).toHaveBeenCalledWith("/api/ask", expect.objectContaining({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: "Тест" })
    }));
  });

  it("uses the backend error message for unsuccessful JSON requests", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: "Модель недоступна" }, false, "Service Unavailable"));

    await expect(postJson("/api/ask", {}, 1_000)).rejects.toThrow("Модель недоступна");
  });

  it("reports a readable timeout for JSON requests", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(pendingUntilAbort());

    const request = postJson("/api/ask", {}, 500);
    const expectation = expect(request).rejects.toThrow("Backend не ответил вовремя. Запрос можно повторить.");
    await vi.advanceTimersByTimeAsync(500);

    await expectation;
  });

  it("sends FormData without a JSON content type", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ chunks: 3 }));
    const body = new FormData();
    body.append("files", new File(["test"], "regulations.docx"));

    await expect(postFormData<{ chunks: number }>("/api/upload", body, 1_000))
      .resolves.toEqual({ chunks: 3 });

    expect(fetchMock).toHaveBeenCalledWith("/api/upload", expect.objectContaining({
      method: "POST",
      body
    }));
    expect(fetchMock.mock.calls[0][1]?.headers).toBeUndefined();
  });

  it("reports a readable timeout for file uploads", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(pendingUntilAbort());

    const request = postFormData("/api/upload", new FormData(), 500);
    const expectation = expect(request).rejects.toThrow("Загрузка не завершилась вовремя. Попробуй меньший файл или повтори позже.");
    await vi.advanceTimersByTimeAsync(500);

    await expectation;
  });
});
