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

  useEffect(() => {
    if (!container.current) return;
    const office = new OfficeScene();
    scene.current = office;
    void office.mount(container.current);
    let frame = 0;
    const publish = () => {
      onAnchors(office.anchors());
      frame = requestAnimationFrame(publish);
    };
    publish();
    return () => {
      cancelAnimationFrame(frame);
      office.dispose();
    };
  }, [onAnchors]);

  useEffect(() => {
    scene.current?.setAgents(snapshot.agents);
  }, [snapshot.agents]);

  return <div className="office" ref={container} />;
}
