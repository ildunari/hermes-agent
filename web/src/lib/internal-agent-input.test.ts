import { describe, expect, it } from "vitest";

import {
  isAsyncDelegationAgentInput,
  isBackgroundProcessAgentInput,
  isInternalAgentInput,
} from "./internal-agent-input";

describe("internal agent input classifiers", () => {
  it("matches async delegation completion payloads", () => {
    const payload = [
      "[ASYNC DELEGATION BATCH COMPLETE — deleg_abc12345]",
      "A background fan-out has finished.",
      "--- RESULT ---",
      "internal payload",
    ].join("\n");

    expect(isAsyncDelegationAgentInput(payload)).toBe(true);
    expect(isInternalAgentInput(payload)).toBe(true);
  });

  it("matches background-process synthetic payloads", () => {
    const payload = "[IMPORTANT: Background process proc_123 completed normally.\nOutput:\nPASS]";

    expect(isBackgroundProcessAgentInput(payload)).toBe(true);
    expect(isInternalAgentInput(payload)).toBe(true);
  });

  it("does not match normal user text", () => {
    expect(isInternalAgentInput("please check this file")).toBe(false);
  });
});