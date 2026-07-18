import { describe, expect, it } from "vitest";

import { formatProfileLabel } from "../lib/profile-label";

describe("formatProfileLabel", () => {
  it("capitalizes the first letter of profile names", () => {
    expect(formatProfileLabel("default")).toBe("Default");
    expect(formatProfileLabel("gpt")).toBe("Gpt");
    expect(formatProfileLabel("research")).toBe("Research");
  });

  it("capitalizes each hyphen-separated segment without changing the profile id", () => {
    expect(formatProfileLabel("browser-agent")).toBe("Browser-Agent");
  });

  it("falls back to Default for blank labels", () => {
    expect(formatProfileLabel("")).toBe("Default");
    expect(formatProfileLabel("   ")).toBe("Default");
  });
});