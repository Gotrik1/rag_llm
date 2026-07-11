export declare function postJson<T>(path: string, body: unknown, timeoutMs: number): Promise<T>;
export declare function postFormData<T>(path: string, body: FormData, timeoutMs: number): Promise<T>;
