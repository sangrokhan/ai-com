import type { AgentView } from "../api/client";
import type { Anchor } from "../scene/Scene";

export function bubbleText(agent: AgentView): string | null {
  if (agent.status === "waiting") return "waiting for your sign-off";
  if (agent.status === "working") return agent.activity;
  return null;
}

export function Overlay({
  agents,
  anchors,
}: {
  agents: AgentView[];
  anchors: Anchor[];
}) {
  const byId = new Map(agents.map((agent) => [agent.agent_id, agent]));

  return (
    <div className="overlay">
      {anchors.map((anchor) => {
        const agent = byId.get(anchor.agentId);
        if (!agent) return null;
        const bubble = bubbleText(agent);
        return (
          <div
            key={anchor.agentId}
            className="marker"
            style={{ left: `${anchor.x}px`, top: `${anchor.y}px` }}
          >
            {bubble && <div className="bubble">{bubble}</div>}
            <div className="pill">
              <span className={`dot ${agent.status}`} />
              {agent.name}
            </div>
          </div>
        );
      })}
    </div>
  );
}
