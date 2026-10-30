import type { AssistantRuntime } from "@assistant-ui/react";

/** Retain an immutable snapshot while all of its own fields remain identical. */
export function stableSnapshot<T extends object>(read: () => T): () => T {
  let previous: T | undefined;
  return () => {
    const next = read();
    if (previous) {
      const keys = Object.keys(next) as (keyof T)[];
      if (keys.length === Object.keys(previous).length
        && keys.every((key) => Object.prototype.hasOwnProperty.call(previous, key) && Object.is(previous![key], next[key]))) {
        return previous;
      }
    }
    previous = next;
    return next;
  };
}

/** Adapt the legacy thread-list bridge to React's stable snapshot contract.
 * core 0.2.x allocates disconnected thread-list snapshots on every read. Keep
 * its subscriptions/actions and all changed fields intact; do not cache data
 * by time or suppress notifications. The application still owns chat identity.
 */
export function withStableThreadListSnapshot(runtime: AssistantRuntime): AssistantRuntime {
  const threads = Object.create(runtime.threads) as AssistantRuntime["threads"];
  Object.defineProperty(threads, "getState", { value: stableSnapshot(() => runtime.threads.getState()) });
  const facade = Object.create(runtime) as AssistantRuntime;
  Object.defineProperty(facade, "threads", { value: threads });
  return facade;
}
