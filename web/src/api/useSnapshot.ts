import { useEffect, useRef, useState } from "react";

import { fetchSnapshot, type Snapshot } from "./client";

export function useSnapshot() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [needsLogin, setNeedsLogin] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const retry = useRef(1000);

  useEffect(() => {
    let source: EventSource | null = null;
    let timer: number | undefined;
    let cancelled = false;

    const start = async () => {
      // Always refetch the full snapshot on (re)connect, so a missed delta
      // cannot leave the scene permanently stale.
      const result = await fetchSnapshot();
      if (cancelled) return;
      if (result.kind === "unauthorized") {
        setNeedsLogin(true);
        return;
      }
      if (result.kind === "error") {
        // A server or network failure is not "wrong password" -- keep
        // whatever snapshot is already on screen and say the connection is
        // unhealthy, then retry with backoff, instead of bouncing the
        // operator to the login form.
        setConnected(false);
        setError(result.detail);
        timer = window.setTimeout(start, retry.current);
        retry.current = Math.min(retry.current * 2, 30000);
        return;
      }
      setError(null);
      setSnapshot(result.snapshot);

      source = new EventSource("/console/stream");
      source.onopen = () => {
        setConnected(true);
        retry.current = 1000;
      };
      source.onmessage = (event) => setSnapshot(JSON.parse(event.data) as Snapshot);
      source.onerror = () => {
        setConnected(false);
        setError("stream disconnected");
        source?.close();
        timer = window.setTimeout(start, retry.current);
        retry.current = Math.min(retry.current * 2, 30000);
      };
    };

    void start();
    return () => {
      cancelled = true;
      source?.close();
      window.clearTimeout(timer);
    };
  }, []);

  return { snapshot, connected, needsLogin, error };
}
