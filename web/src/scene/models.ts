import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

const loader = new GLTFLoader();

/** Loads a model, falling back to a box so one missing file cannot blank the view. */
export async function loadModel(url: string, fallbackColor: number): Promise<THREE.Object3D> {
  try {
    const gltf = await loader.loadAsync(url);
    return gltf.scene;
  } catch {
    console.warn(`model missing, using placeholder: ${url}`);
    return new THREE.Mesh(
      new THREE.BoxGeometry(1, 1, 1),
      new THREE.MeshLambertMaterial({ color: fallbackColor }),
    );
  }
}
