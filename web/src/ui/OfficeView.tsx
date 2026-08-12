import { useEffect, useRef } from "react";

import type { Snapshot } from "../api/client";
import { type Anchor, OfficeScene } from "../scene/Scene";

export function OfficeView({
  snapshot,
  onAnchors,
}: {
  snapshot: Snapshot;
  onAnchors: (anchors: Anchor[]) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const scene = useRef<OfficeScene>();
  // Callers (App today, Task 6's overlay later) legitimately pass a new
  // onAnchors closure on every render -- that identity says nothing about
  // whether the scene itself should remount. Route calls through a ref
  // that the effect below reads, updated on every render but never part
  // of a dependency array, so the mount effect can stay independent of
  // the callback's identity.
  const onAnchorsRef = useRef(onAnchors);
  onAnchorsRef.current = onAnchors;

  // Mount-once effect. This MUST NOT depend on `onAnchors` (or on any
  // value that changes when `snapshot` changes, since App re-renders on
  // every snapshot tick, ~every 10s): OfficeScene.positions is instance
  // state that gives agents their stable desk across snapshots, and a new
  // WebGLRenderer plus re-fetching both .glb models on every remount would
  // wipe it, silently turning "desks are stable across reloads" into
  // "desks are stable only by coincidence of the deterministic hash and a
  // stable agent order." The empty dependency array is what makes the
  // scene mount once and live for the component's lifetime.
  useEffect(() => {
    if (!container.current) return;
    const office = new OfficeScene();
    scene.current = office;
    void office.mount(container.current);
    // office.anchors() projects every seated agent's world position through
    // the camera -- real work, not free. The camera is orthographic and
    // fixed once seated (it only moves on centreCamera/resize, both rare),
    // so anchor positions barely change between frames. Anything faster
    // than App's own ANCHOR_COMMIT_MS=100 throttle is wasted: those extra
    // projections would just be dropped on the floor before ever reaching
    // state. Publishing on an interval instead of every animation frame
    // (60Hz) cuts that wasted work by roughly an order of magnitude while
    // staying imperceptible for text labels.
    const PUBLISH_MS = 100;
    const interval = window.setInterval(() => {
      onAnchorsRef.current(office.anchors());
    }, PUBLISH_MS);
    return () => {
      window.clearInterval(interval);
      office.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- see comment above
  }, []);

  useEffect(() => {
    scene.current?.setAgents(snapshot.agents);
  }, [snapshot.agents]);

  return <div className="office" ref={container} />;
}
