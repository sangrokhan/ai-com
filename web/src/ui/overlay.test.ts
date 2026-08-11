import { describe, expect, it } from "vitest";

import type { AgentView } from "../api/client";
import { bubbleText } from "./Overlay";

const base: AgentView = {
  agent_id: "a",
  name: "researcher",
  status: "idle",
  run_id: null,
  activity: null,
};

describe("bubbleText", () => {
  it("shows the activity while working", () => {
    expect(bubbleText({ ...base, status: "working", activity: "using WebSearch" })).toBe(
      "using WebSearch",
    );
  });

  it("says nothing when idle", () => {
    expect(bubbleText(base)).toBeNull();
  });

  it("announces that a sign-off is waiting", () => {
    const text = bubbleText({ ...base, status: "waiting" });
    expect(text).not.toBeNull();
    expect(text!.length).toBeGreaterThan(0);
  });

  it("says nothing while working with no activity yet", () => {
    expect(bubbleText({ ...base, status: "working", activity: null })).toBeNull();
  });
});
