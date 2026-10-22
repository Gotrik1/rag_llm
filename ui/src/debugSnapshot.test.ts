import { describe, expect, it } from "vitest";

import { contextFromCompletedJob, preserveContextSnapshot } from "./debugSnapshot";

const chunk = { file: "расчёт.docx", text: "Точная цитата\nСебестоимость (УУ)", score: 0.76, extra: { revision: "v1" } };
type Answer = { chat_id?: string; message_id?: string; job_id?: string; context?: typeof chunk[] };
const isChunk = (value: unknown): value is typeof chunk => typeof value === "object" && value !== null && "text" in value;
const expected = { jobId: "job-1", messageId: "answer-1" };
const validJob = () => ({
  id: "job-1", kind: "generation", status: "completed",
  result: { job_id: "job-1", message_id: "answer-1", status: "complete", context: [chunk] }
});

describe("current answer's original evidence snapshot", () => {
  it.each([undefined, "job-1"])("keeps the exact array for the same answer and job binding (job=%s)", (jobId) => {
    const context = [chunk];
    const restored: Answer = { chat_id: "chat-1", message_id: "answer-1", job_id: jobId };
    const merged = preserveContextSnapshot(restored, { ...restored, context });
    expect(merged?.context).toBe(context);
    expect(merged?.context?.[0]).toBe(chunk);
  });

  it.each([
    { restoredJobId: "job-2", currentJobId: "job-1" },
    { restoredJobId: undefined, currentJobId: "job-1" },
    { restoredJobId: "job-1", currentJobId: undefined }
  ])("never carries evidence across different or missing job bindings %j", ({ restoredJobId, currentJobId }) => {
    const restored: Answer = { chat_id: "chat-1", message_id: "answer-1", job_id: restoredJobId };
    const current: Answer = { ...restored, job_id: currentJobId, context: [chunk] };
    expect(preserveContextSnapshot(restored, current)).toBe(restored);
    expect(restored.context).toBeUndefined();
  });

  it("preserves a genuine empty array instead of treating it as a missing snapshot", () => {
    const context: typeof chunk[] = [];
    const answer: Answer = { chat_id: "chat-1", message_id: "answer-1" };
    expect(preserveContextSnapshot(answer, { ...answer, context })?.context).toBe(context);
    expect(preserveContextSnapshot(answer, answer)?.context).toBeUndefined();
  });

  it.each([
    { chat_id: "chat-1", message_id: "answer-2" },
    { chat_id: "chat-2", message_id: "answer-1" },
    { message_id: "answer-1" },
    { chat_id: "chat-1" }
  ])("never carries evidence over to a different or unbound answer %j", (restored) => {
    expect(preserveContextSnapshot<Answer>(restored, { chat_id: "chat-1", message_id: "answer-1", context: [chunk] })).toBe(restored);
  });

  it("does not overwrite a newly supplied snapshot", () => {
    const restored = { chat_id: "chat-1", message_id: "answer-1", context: [] };
    expect(preserveContextSnapshot(restored, { ...restored, context: [chunk] })).toBe(restored);
    expect(preserveContextSnapshot(null, restored)).toBeNull();
  });
});

describe("durable original evidence recovery", () => {
  it("returns the original array unchanged, including order, extra fields and exact quotes", () => {
    const job = validJob();
    const context = contextFromCompletedJob(job, expected, isChunk);
    expect(context).toBe(job.result.context);
    expect(context[0]).toBe(chunk);
  });

  it("accepts genuinely empty original evidence", () => {
    const job = validJob();
    job.result.context = [];
    expect(contextFromCompletedJob(job, expected, isChunk)).toBe(job.result.context);
  });

  it.each([
    ["missing job", null],
    ["wrong job id", { ...validJob(), id: "job-other" }],
    ["wrong kind", { ...validJob(), kind: "ingestion" }],
    ["running job", { ...validJob(), status: "running" }],
    ["failed job", { ...validJob(), status: "failed" }],
    ["cancelled job", { ...validJob(), status: "cancelled" }],
    ["missing result", { ...validJob(), result: undefined }],
    ["wrong result message", { ...validJob(), result: { ...validJob().result, message_id: "answer-other" } }],
    ["wrong result job", { ...validJob(), result: { ...validJob().result, job_id: "job-other" } }],
    ["missing result job", { ...validJob(), result: { ...validJob().result, job_id: undefined } }],
    ["running result", { ...validJob(), result: { ...validJob().result, status: "running" } }],
    ["missing result status", { ...validJob(), result: { ...validJob().result, status: undefined } }]
  ])("rejects %s before exposing any context", (_label, job) => {
    expect(() => contextFromCompletedJob(job, expected, isChunk)).toThrow("не соответствует");
  });

  it.each([undefined, null, {}, [chunk, null], [chunk, "invalid"]])("rejects malformed evidence atomically %j", (context) => {
    const job = { ...validJob(), result: { ...validJob().result, context } };
    expect(() => contextFromCompletedJob(job, expected, isChunk)).toThrow("отсутствует или повреждён");
  });
});
