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

export async function fetchSnapshot(): Promise<Snapshot | null> {
  const response = await fetch("/console/state");
  if (!response.ok) return null;
  return (await response.json()) as Snapshot;
}
