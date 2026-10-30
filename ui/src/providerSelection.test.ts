import { describe, expect, it } from "vitest";
import { providerConfigForSelection } from "./App";

const defaults = {
  ollama: { base_url: "http://configured-bridge.example.test:11434", model: "server-default" },
  gigachat: { base_url: "https://cloud.example.test/api", model: "cloud-model" }
};

describe("provider selection endpoint ownership", () => {
  it("switches cloud to the server-advertised Ollama endpoint", () => {
    const next = providerConfigForSelection({ provider: "gigachat", model: "old-cloud",
      base_url: "https://old-cloud.example.test/api", has_api_key: true, api_key_hint: "old-hint" },
    "ollama", defaults, ["installed-model"]);
    expect(next.provider).toBe("ollama");
    expect(next.base_url).toBe(defaults.ollama.base_url);
    expect(next.model).toBe("installed-model");
    expect(next.has_api_key).toBe(false);
    expect(next.api_key_hint).toBe("");
  });

  it("preserves an explicit custom Ollama endpoint when selecting the same provider", () => {
    const current = { provider: "ollama" as const, model: "custom-model",
      base_url: "https://custom-ollama.example.test/prefix", auth_type: "none" };
    expect(providerConfigForSelection(current, "ollama", defaults, ["other-model"])).toEqual(current);
  });

  it("never inherits another provider endpoint when an older server omits the preset", () => {
    const next = providerConfigForSelection({ provider: "gigachat", model: "old-cloud",
      base_url: defaults.gigachat.base_url }, "ollama", {}, []);
    expect(next.base_url).toBe("");
    expect(next.model).toBe("");
  });

  it("uses the advertised default model if installed-model discovery is temporarily empty", () => {
    const next = providerConfigForSelection({ provider: "gigachat", model: "old-cloud" },
      "ollama", defaults, []);
    expect(next.model).toBe(defaults.ollama.model);
  });
});
