import { useSnapshot } from "./api/useSnapshot";
import { AgentTable } from "./ui/AgentTable";
import { Login } from "./ui/Login";

export function App() {
  const { snapshot, connected, needsLogin } = useSnapshot();

  if (needsLogin) return <Login />;
  if (!snapshot) return <p className="loading">connecting…</p>;

  return (
    <div className="console">
      <header>
        <span className={connected ? "live" : "stale"}>
          {connected ? "● live" : "○ reconnecting"}
        </span>
        <span>{snapshot.agents.length} agents</span>
        {snapshot.pending_approvals > 0 && (
          <span className="pending">{snapshot.pending_approvals} awaiting sign-off</span>
        )}
        {snapshot.paused_until && <span className="paused">paused</span>}
      </header>
      <AgentTable snapshot={snapshot} />
    </div>
  );
}
