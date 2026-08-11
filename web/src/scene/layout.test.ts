import { describe, expect, it } from "vitest";

import { deskPosition, deskSlot } from "./layout";

describe("deskSlot", () => {
  it("is stable for the same agent id", () => {
    expect(deskSlot("agent-a", 8)).toEqual(deskSlot("agent-a", 8));
  });

  it("gives different agents different slots when there is room", () => {
    const slots = ["a", "b", "c", "d"].map((id) => JSON.stringify(deskSlot(id, 16)));
    expect(new Set(slots).size).toBe(4);
  });

  it("stays inside the grid", () => {
    for (const id of ["a", "b", "c", "d", "e", "f", "g", "h", "i"]) {
      const slot = deskSlot(id, 9);
      expect(slot.row).toBeGreaterThanOrEqual(0);
      expect(slot.col).toBeGreaterThanOrEqual(0);
      expect(slot.row).toBeLessThan(3);
      expect(slot.col).toBeLessThan(3);
    }
  });
});

describe("deskPosition", () => {
  it("spaces desks apart", () => {
    const a = deskPosition({ row: 0, col: 0 });
    const b = deskPosition({ row: 0, col: 1 });
    expect(Math.abs(a.x - b.x)).toBeGreaterThan(1);
  });
});
