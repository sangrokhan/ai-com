export type AgentView = {
  agent_id: string;
  name: string;
  status: "waiting" | "working" | "paused" | "failed" | "idle";
  run_id: string | null;
  activity: string | null;
};

export type Snapshot = {
  generated_at: string;
  paused_until: string | null;
  pending_approvals: number;
  agents: AgentView[];
};

export async function login(password: string): Promise<boolean> {
  const response = await fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  return response.ok;
}

export type SnapshotResult =
  | { kind: "ok"; snapshot: Snapshot }
  | { kind: "unauthorized" }
  | { kind: "error"; detail: string };

export async function fetchSnapshot(): Promise<SnapshotResult> {
  let response: Response;
  try {
    response = await fetch("/console/state");
  } catch {
    // Network-level failure (server down, connection dropped): distinct from
    // a bad password, so the caller must not send the operator to the login
    // screen for this.
    return { kind: "error", detail: "network error" };
  }
  if (response.status === 401) return { kind: "unauthorized" };
  if (!response.ok) return { kind: "error", detail: `HTTP ${response.status}` };
  return { kind: "ok", snapshot: (await response.json()) as Snapshot };
}
