/** Durable answer evidence is independent of retrieval rank: native reads have none. */
export type ApiContextChunk = {
  file: string;
  section_path?: string;
  chunk_type?: string;
  status?: string;
  score?: number | null;
  text: string;
  html_text?: string;
  flow_origin?: "python" | "rust";
};

/** Validate mandatory source content; a present rank must be a finite number. */
export function isApiContextChunk(value: unknown): value is ApiContextChunk {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const chunk = value as Record<string, unknown>;
  return typeof chunk.file === "string" && typeof chunk.text === "string"
    && (chunk.score == null || (typeof chunk.score === "number" && Number.isFinite(chunk.score)));
}

type SnapshotCarrier = {
  chat_id?: string;
  message_id?: string;
  job_id?: string;
  context?: unknown[];
};

/** Keep only the currently displayed answer's original, in-memory evidence. */
export function preserveContextSnapshot<T extends SnapshotCarrier>(restored: T | null, current: T | null): T | null {
  if (
    !restored || !current || !restored.chat_id || !restored.message_id
    || restored.chat_id !== current.chat_id || restored.message_id !== current.message_id
    || restored.job_id !== current.job_id
    || Array.isArray(restored.context) || !Array.isArray(current.context)
  ) return restored;
  return { ...restored, context: current.context };
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Validate identity before exposing the exact durable result; never re-retrieve. */
export function contextFromCompletedJob<T>(
  job: unknown,
  expected: { jobId: string; messageId: string },
  isChunk: (value: unknown) => value is T
): T[] {
  if (
    !record(job) || job.id !== expected.jobId || job.kind !== "generation" || job.status !== "completed"
    || !record(job.result) || job.result.message_id !== expected.messageId
    || job.result.job_id !== expected.jobId || job.result.status !== "complete"
  ) {
    throw new Error("Сохранённый результат не соответствует этому ответу. Контекст не показан; попробуйте загрузить его ещё раз.");
  }
  const context = job.result.context;
  if (!Array.isArray(context) || !context.every(isChunk)) {
    throw new Error("Исходный контекст отсутствует или повреждён в сохранённом результате. Новый поиск не выполнялся.");
  }
  // Preserve the array and every original field, order, quote and score.
  return context;
}
