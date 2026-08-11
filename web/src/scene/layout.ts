export type Slot = { row: number; col: number };

export const DESK_SPACING = 3;

/** Stable hash so an agent keeps its desk across reloads and restarts. */
function hash(value: string): number {
  let h = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export function gridSize(total: number): number {
  return Math.max(1, Math.ceil(Math.sqrt(Math.max(total, 1))));
}

export function deskSlot(agentId: string, total: number): Slot {
  const size = gridSize(total);
  const index = hash(agentId) % (size * size);
  return { row: Math.floor(index / size), col: index % size };
}

export function deskPosition(slot: Slot): { x: number; z: number } {
  return { x: slot.col * DESK_SPACING, z: slot.row * DESK_SPACING };
}

function key(slot: Slot): string {
  return `${slot.row}:${slot.col}`;
}

/**
 * Resolves hash collisions by walking to the next free cell, wrapping
 * row-major through the grid. `taken` must contain the cells of every
 * agent already seated (not just ones assigned in the current pass) or
 * a new agent can land on an occupied desk.
 *
 * Pure and side-effect free: it does not mutate `taken`, so the caller
 * decides when/whether to record the returned slot as occupied.
 */
export function freeSlot(agentId: string, total: number, taken: ReadonlySet<string>): Slot {
  const size = gridSize(total);
  let slot = deskSlot(agentId, total);
  let guard = 0;
  while (taken.has(key(slot)) && guard < size * size) {
    const next = slot.col + 1;
    slot = { row: (slot.row + Math.floor(next / size)) % size, col: next % size };
    guard += 1;
  }
  return slot;
}

export function slotKey(slot: Slot): string {
  return key(slot);
}
