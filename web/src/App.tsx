import { useRef, useState } from "react";

import { useSnapshot } from "./api/useSnapshot";
import type { Anchor } from "./scene/Scene";
import { AgentTable } from "./ui/AgentTable";
import { Login } from "./ui/Login";
import { OfficeView } from "./ui/OfficeView";
import { Overlay } from "./ui/Overlay";

// OfficeView's onAnchors fires on every animation frame (~60Hz). Piping that
// straight into useState would re-render the whole App -- header, counts,
// everything -- 60 times a second for label positions that only need to
// track a seated, roughly-static camera. Instead we keep the latest anchors
// in a ref and commit to state on a fixed cadence, which is imperceptible
// for text labels but cuts render pressure by roughly two orders of
// magnitude. This is entirely internal to App: the OfficeView/Overlay
// contract (a plain `(Anchor[]) => void` callback) is unchanged.
const ANCHOR_COMMIT_MS = 100;

function useThrottledAnchors(): [Anchor[], (anchors: Anchor[]) => void] {
  const [anchors, setAnchors] = useState<Anchor[]>([]);
  const lastCommit = useRef(0);
  const onAnchors = (next: Anchor[]) => {
    const now = performance.now();
    if (now - lastCommit.current < ANCHOR_COMMIT_MS) return;
    lastCommit.current = now;
    setAnchors(next);
  };
  return [anchors, onAnchors];
}

function webglAvailable(): boolean {
  try {
    return !!document.createElement("canvas").getContext("webgl2");
  } catch {
    return false;
  }
}

export function App() {
  const { snapshot, connected, needsLogin, error } = useSnapshot();
  const [anchors, onAnchors] = useThrottledAnchors();

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
        <div className="stage">
          <OfficeView snapshot={snapshot} onAnchors={onAnchors} />
          <Overlay agents={snapshot.agents} anchors={anchors} />
        </div>
      ) : (
        <AgentTable snapshot={snapshot} />
      )}
    </div>
  );
}
