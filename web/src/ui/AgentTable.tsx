import type { Snapshot } from "../api/client";

export function AgentTable({ snapshot }: { snapshot: Snapshot }) {
  return (
    <table className="agents">
      <tbody>
        {snapshot.agents.map((agent) => (
          <tr key={agent.agent_id}>
            <td>
              <span className={`dot ${agent.status}`} />
            </td>
            <td>{agent.name}</td>
            <td>{agent.status}</td>
            <td>{agent.activity ?? ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
