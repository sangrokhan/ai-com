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
