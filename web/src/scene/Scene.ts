import * as THREE from "three";

import type { AgentView } from "../api/client";
import { DESK_SPACING, deskPosition, freeSlot, gridSize, type Slot, slotKey } from "./layout";
import { loadModel } from "./models";

export type Anchor = { agentId: string; x: number; y: number };

export class OfficeScene {
  private renderer?: THREE.WebGLRenderer;
  private readonly scene = new THREE.Scene();
  private camera = new THREE.OrthographicCamera();
  private readonly people = new Map<string, THREE.Object3D>();
  private readonly positions = new Map<string, Slot>();
  private desk?: THREE.Object3D;
  private character?: THREE.Object3D;
  private frame = 0;
  private container?: HTMLElement;

  async mount(container: HTMLElement): Promise<void> {
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    container.appendChild(this.renderer.domElement);

    this.scene.background = new THREE.Color(0xdfe7dd);
    this.scene.add(new THREE.AmbientLight(0xffffff, 1.4));
    const sun = new THREE.DirectionalLight(0xffffff, 1.2);
    sun.position.set(6, 12, 8);
    this.scene.add(sun);

    this.desk = await loadModel("/models/desk.glb", 0xc9c2b6);
    this.character = await loadModel("/models/character.glb", 0x8fa7c4);

    this.resize();
    window.addEventListener("resize", this.resize);
    this.renderLoop();
  }

  setAgents(agents: AgentView[]): void {
    const wanted = new Set(agents.map((a) => a.agent_id));
    for (const [id, object] of this.people) {
      if (!wanted.has(id)) {
        this.scene.remove(object);
        this.people.delete(id);
        this.positions.delete(id);
      }
    }

    // Seed with every currently seated agent's cell so a newly added agent
    // can never be placed on top of one that is already sitting there.
    const taken = new Set<string>();
    for (const slot of this.positions.values()) {
      taken.add(slotKey(slot));
    }

    for (const agent of agents) {
      if (this.people.has(agent.agent_id)) continue;
      const slot = freeSlot(agent.agent_id, agents.length, taken);
      taken.add(slotKey(slot));
      this.positions.set(agent.agent_id, slot);
      const { x, z } = deskPosition(slot);

      const group = new THREE.Group();
      if (this.desk) {
        const desk = this.desk.clone();
        desk.position.set(0, 0, 0);
        group.add(desk);
      }
      if (this.character) {
        const person = this.character.clone();
        person.position.set(0, 0, 0.8);
        group.add(person);
      }
      group.position.set(x, 0, z);
      this.scene.add(group);
      this.people.set(agent.agent_id, group);
    }
    this.centreCamera(agents.length);
  }

  anchors(): Anchor[] {
    if (!this.renderer) return [];
    const rect = this.renderer.domElement.getBoundingClientRect();
    const result: Anchor[] = [];
    const vector = new THREE.Vector3();
    for (const [agentId, object] of this.people) {
      vector.set(0, 1.8, 0);
      object.localToWorld(vector);
      vector.project(this.camera);
      result.push({
        agentId,
        x: ((vector.x + 1) / 2) * rect.width,
        y: ((1 - vector.y) / 2) * rect.height,
      });
    }
    return result;
  }

  dispose(): void {
    cancelAnimationFrame(this.frame);
    window.removeEventListener("resize", this.resize);
    this.renderer?.dispose();
    this.renderer?.domElement.remove();
  }

  private centreCamera(total: number): void {
    const span = gridSize(total) * DESK_SPACING;
    this.camera.position.set(span + 8, span + 10, span + 8);
    this.camera.lookAt(span / 2, 0, span / 2);
    this.resize();
  }

  private readonly resize = (): void => {
    if (!this.renderer || !this.container) return;
    const { clientWidth: w, clientHeight: h } = this.container;
    this.renderer.setSize(w, h);
    const view = 14;
    const aspect = w / Math.max(h, 1);
    this.camera.left = -view * aspect;
    this.camera.right = view * aspect;
    this.camera.top = view;
    this.camera.bottom = -view;
    this.camera.near = -100;
    this.camera.far = 200;
    this.camera.updateProjectionMatrix();
  };

  private renderLoop = (): void => {
    this.frame = requestAnimationFrame(this.renderLoop);
    this.renderer?.render(this.scene, this.camera);
  };
}
