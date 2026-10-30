import type { AssistantRuntime } from "@assistant-ui/react";
import { describe, expect, it, vi } from "vitest";

import { stableSnapshot, withStableThreadListSnapshot } from "./runtimeSnapshot";

describe("immutable runtime snapshot compatibility", () => {
  it("retains identity for repeated reads and exposes each changed field immediately", () => {
    let ids = ["chat-a"];
    let isLoading = false;
    const read = stableSnapshot(() => ({ ids, isLoading }));
    const initial = read();
    expect(read()).toBe(initial);
    isLoading = true;
    const loading = read();
    expect(loading).not.toBe(initial);
    expect(loading.isLoading).toBe(true);
    expect(read()).toBe(loading);
    ids = ["chat-b"];
    expect(read().ids).toBe(ids);
    expect(read()).not.toBe(loading);
  });

  it("does not conflate missing fields with undefined or treat NaN as a changing value", () => {
    let state: { value?: number } = { value: NaN };
    const read = stableSnapshot(() => ({ ...state }));
    expect(read()).toBe(read());
    state = { value: undefined };
    const explicitUndefined = read();
    state = {};
    expect(read()).not.toBe(explicitUndefined);
  });

  it("preserves subscriptions and actions without mutating the dependency runtime", () => {
    const ids = ["chat-a"];
    let loading = false;
    let notify = () => {};
    const unsubscribe = vi.fn();
    const subscribe = vi.fn((callback: () => void) => { notify = callback; return unsubscribe; });
    const switchToThread = vi.fn();
    const source = {
      threads: { getState: () => ({ ids, loading }), subscribe, switchToThread },
      thread: {}, registerModelContextProvider: vi.fn()
    };
    const facade = withStableThreadListSnapshot(source as unknown as AssistantRuntime);
    expect(source.threads.getState()).not.toBe(source.threads.getState());
    expect(facade.threads.getState()).toBe(facade.threads.getState());
    expect(facade.thread).toBe(source.thread);
    const listener = vi.fn();
    const stop = facade.threads.subscribe(listener);
    loading = true;
    notify();
    expect(listener).toHaveBeenCalledOnce();
    expect(facade.threads.getState()).toEqual({ ids, loading: true });
    facade.threads.switchToThread("chat-b");
    expect(switchToThread).toHaveBeenCalledWith("chat-b");
    stop();
    expect(unsubscribe).toHaveBeenCalledOnce();
  });
});
