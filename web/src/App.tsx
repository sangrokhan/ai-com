import { useSnapshot } from "./api/useSnapshot";
import { AgentTable } from "./ui/AgentTable";
import { Login } from "./ui/Login";
import { OfficeView } from "./ui/OfficeView";

function webglAvailable(): boolean {
  try {
    return !!document.createElement("canvas").getContext("webgl2");
  } catch {
    return false;
  }
}

export function App() {
  const { snapshot, connected, needsLogin, error } = useSnapshot();

  if (needsLogin) return <Login />;
  if (!snapshot) {
    // No snapshot yet: either still connecting for the first time, or the
    // first fetch itself failed (server down / network error) -- say which.
    return <p className="loading">{error ? `connection unhealthy: ${error}` : "connecting…"}</p>;
  }

  return (
    <div className="console">
      <header>
        <span className={connected ? "live" : "stale"}>
          {connected ? "● live" : `○ ${error ?? "reconnecting"}`}
        </span>
        <span>{snapshot.agents.length} agents</span>
        {snapshot.pending_approvals > 0 && (
          <span className="pending">{snapshot.pending_approvals} awaiting sign-off</span>
        )}
        {snapshot.paused_until && <span className="paused">paused</span>}
      </header>
      {webglAvailable() ? (
        <OfficeView snapshot={snapshot} onAnchors={() => undefined} />
      ) : (
        <AgentTable snapshot={snapshot} />
      )}
    </div>
  );
}
