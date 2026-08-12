import { describe, expect, it } from "vitest";

import { deskPosition, deskSlot, freeSlot, gridSize, slotKey } from "./layout";

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

describe("freeSlot", () => {
  // "agent-0" and "agent-4" both hash to {row: 1, col: 1} at total=4 (a
  // 2x2 grid) — a genuine collision, not a contrived one. Confirmed by
  // brute force: deskSlot("agent-0", 4) === deskSlot("agent-4", 4).
  it("gives a colliding agent a different cell than the one already seated", () => {
    expect(deskSlot("agent-0", 4)).toEqual(deskSlot("agent-4", 4));

    const seated = deskSlot("agent-0", 4);
    const taken = new Set([slotKey(seated)]);
    const resolved = freeSlot("agent-4", 4, taken);

    expect(resolved).not.toEqual(seated);
  });

  it("never returns a cell already in the taken set when a free cell exists", () => {
    const total = 4;
    const size = gridSize(total);
    // Seat every cell except one.
    const taken = new Set<string>();
    for (let row = 0; row < size; row += 1) {
      for (let col = 0; col < size; col += 1) {
        if (row === size - 1 && col === size - 1) continue; // leave one free
        taken.add(slotKey({ row, col }));
      }
    }

    const resolved = freeSlot("agent-4", total, taken);
    expect(taken.has(slotKey(resolved))).toBe(false);
    expect(resolved).toEqual({ row: size - 1, col: size - 1 });
  });

  it("an already-seated agent keeps its exact cell when other agents are added", () => {
    // Simulates what OfficeScene.setAgents does: agent-0 seats first, then
    // agent-4 (which collides with it) is added in a later pass. agent-0's
    // cell must already be in `taken` for agent-4's placement, and must
    // never move.
    const seatedSlot = deskSlot("agent-0", 4);
    const taken = new Set([slotKey(seatedSlot)]);

    const newSlot = freeSlot("agent-4", 4, taken);
    taken.add(slotKey(newSlot));

    // agent-0's cell, recomputed independently, is unchanged.
    expect(deskSlot("agent-0", 4)).toEqual(seatedSlot);
    // and the two agents do not share a cell.
    expect(newSlot).not.toEqual(seatedSlot);
  });

  it("terminates even when the grid is genuinely full", () => {
    const total = 4;
    const size = gridSize(total);
    const taken = new Set<string>();
    for (let row = 0; row < size; row += 1) {
      for (let col = 0; col < size; col += 1) {
        taken.add(slotKey({ row, col }));
      }
    }

    // Every cell is taken, so no free cell exists. The walk is bounded by
    // `guard < size * size`, so this must return promptly rather than
    // looping forever -- but because the grid truly has no room, the
    // returned slot is necessarily still a member of `taken`. In practice
    // this never happens from OfficeScene.setAgents, because the grid is
    // always sized to fit agents.length, so there are always at least as
    // many cells as agents being seated.
    const start = Date.now();
    const resolved = freeSlot("agent-99", total, taken);
    expect(Date.now() - start).toBeLessThan(50);
    expect(resolved.row).toBeGreaterThanOrEqual(0);
    expect(resolved.col).toBeGreaterThanOrEqual(0);
    expect(taken.has(slotKey(resolved))).toBe(true);
  });
});
