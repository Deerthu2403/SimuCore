import { useEffect, useRef } from "react";
import { useThree } from "@react-three/fiber";
import { CameraControls } from "@react-three/drei";
import Controls from "camera-controls";
import { Box3, PerspectiveCamera, Vector3 } from "three";
import { useView, type CameraRequest, type CameraStep, type Preset } from "../twin/view";
import { world } from "./world";

const DIRECTION: Record<Preset, [azimuth: number, polar: number]> = {
  overview: [-0.3, 0.98],
  top: [0, 0.02],
  front: [0, 1.24],
  side: [-Math.PI / 2 + 0.12, 1.1],
};

const BOX = new Box3();

/** The distance at which a box fills the view, looked at from a direction. */
export function fitDistance(box: Box3, azimuth: number, polar: number, fov: number, aspect: number, margin = 1.05): { center: Vector3; distance: number } {
  let center = box.getCenter(new Vector3());
  const back = new Vector3(Math.sin(polar) * Math.sin(azimuth), Math.cos(polar), Math.sin(polar) * Math.cos(azimuth));
  const right = new Vector3(Math.cos(azimuth), 0, -Math.sin(azimuth));
  const up = new Vector3().crossVectors(back, right);
  const tanV = Math.tan((fov * Math.PI) / 360) / margin;
  const tanH = tanV * aspect;
  let distance = 4;
  const p = new Vector3();
  // Perspective makes the near side larger, so the middle of the box is not the middle of its
  // picture: the target is moved until it is.
  for (let round = 0; round < 6; round++) {
    distance = 4;
    const corners: [number, number, number][] = [];
    for (let i = 0; i < 8; i++) {
      p.set(i & 1 ? box.max.x : box.min.x, i & 2 ? box.max.y : box.min.y, i & 4 ? box.max.z : box.min.z).sub(center);
      const c: [number, number, number] = [p.dot(right), p.dot(up), p.dot(back)];
      corners.push(c);
      distance = Math.max(distance, c[2] + Math.abs(c[0]) / tanH, c[2] + Math.abs(c[1]) / tanV);
    }
    let [x0, x1, y0, y1] = [Infinity, -Infinity, Infinity, -Infinity];
    for (const [x, y, z] of corners) {
      const s = 1 / (distance - z);
      x0 = Math.min(x0, x * s);
      x1 = Math.max(x1, x * s);
      y0 = Math.min(y0, y * s);
      y1 = Math.max(y1, y * s);
    }
    center = center.clone().addScaledVector(right, ((x0 + x1) / 2) * distance * 0.8).addScaledVector(up, ((y0 + y1) / 2) * distance * 0.8);
  }
  return { center, distance };
}

/** Mouse: left rotates, right pans, wheel zooms towards the pointer. Touch: one finger rotates, two
 *  zoom and pan. All requests from buttons and keys end here. */
export function CameraRig({ reduced }: { reduced: boolean }) {
  const ref = useRef<CameraControls>(null);
  const done = useRef(-1);
  const { camera, size } = useThree();
  const req = useView((s) => s.camera);
  const bounds = world.layout.bounds;

  useEffect(() => {
    const c = ref.current!;
    c.mouseButtons.right = Controls.ACTION.TRUCK;
    c.mouseButtons.wheel = Controls.ACTION.DOLLY;
    c.mouseButtons.middle = Controls.ACTION.TRUCK;
    c.touches.two = Controls.ACTION.TOUCH_DOLLY_TRUCK;
    c.minDistance = 2.5;
    c.maxPolarAngle = Math.PI / 2 - 0.03;
    c.dollyToCursor = true;
    c.azimuthRotateSpeed = c.polarRotateSpeed = 0.6;
    rig = c;
    return () => {
      rig = null;
    };
  }, []);

  useEffect(() => {
    const c = ref.current!;
    c.smoothTime = reduced ? 0.001 : 0.32;
    c.draggingSmoothTime = reduced ? 0.001 : 0.1;
  }, [reduced]);

  // The camera cannot get lost: its target stays inside the hall.
  useEffect(() => {
    const c = ref.current!;
    const pad = 14;
    c.maxDistance = Math.max(60, (bounds.x1 - bounds.x0 + bounds.z1 - bounds.z0) * 2.2);
    c.setBoundary(new Box3(new Vector3(bounds.x0 - pad, 0, bounds.z0 - pad), new Vector3(bounds.x1 + pad, 8, bounds.z1 + pad)));
    camera.far = c.maxDistance * 2 + 200;
    camera.updateProjectionMatrix();
  }, [bounds, camera]);

  useEffect(() => {
    if (done.current === req.seq || size.width < 10) return;
    const animate = !reduced && done.current !== -1;
    done.current = req.seq;
    handle(ref.current!, req.req, camera as PerspectiveCamera, size.width / size.height, animate);
  }, [req, camera, size, reduced]);

  return <CameraControls ref={ref} makeDefault />;
}

let rig: CameraControls | null = null;
/** For the test hook only. */
export function cameraState() {
  if (!rig) return null;
  const t = rig.getTarget(new Vector3(), false);
  const p = rig.getPosition(new Vector3(), false);
  return { azimuth: rig.azimuthAngle, polar: rig.polarAngle, distance: rig.distance, target: t.toArray(), position: p.toArray(), active: rig.active };
}

function handle(c: CameraControls, r: CameraRequest, camera: PerspectiveCamera, aspect: number, animate: boolean): void {
  if (r.what === "step") return step(c, r.name, animate);
  let ids: Iterable<string>;
  let azimuth: number;
  let polar: number;
  if (r.what === "preset") {
    ids = [...world.nodes.values()].filter((n) => n.toVisible > 0).map((n) => n.id);
    [azimuth, polar] = DIRECTION[r.name];
  } else {
    ids = r.ids;
    azimuth = c.azimuthAngle;
    polar = Math.min(Math.max(c.polarAngle, 0.55), 1.2);
    // the direction is kept, so that the user does not lose orientation
  }
  world.targetBox(ids, BOX);
  if (BOX.isEmpty()) return;
  if (r.what === "fit" && r.ids.length === 1) BOX.expandByVector(new Vector3(2.6, 0.5, 2.6));
  const fit = fitDistance(BOX, azimuth, polar, camera.fov, aspect);
  // shortest way round
  const turns = Math.round((c.azimuthAngle - azimuth) / (Math.PI * 2));
  void c.setFocalOffset(0, 0, 0, animate);
  void c.moveTo(fit.center.x, fit.center.y, fit.center.z, animate);
  void c.rotateTo(azimuth + turns * Math.PI * 2, polar, animate);
  void c.dollyTo(fit.distance, animate);
}

function step(c: CameraControls, name: CameraStep, animate: boolean): void {
  const d = c.distance;
  const turn = Math.PI / 12;
  switch (name) {
    case "zoomIn": void c.dollyTo(d * 0.78, animate); break;
    case "zoomOut": void c.dollyTo(d / 0.78, animate); break;
    case "left": void c.rotate(-turn, 0, animate); break;
    case "right": void c.rotate(turn, 0, animate); break;
    case "up": void c.rotate(0, -turn * 0.6, animate); break;
    case "down": void c.rotate(0, turn * 0.6, animate); break;
    case "panLeft": void c.truck(-d * 0.1, 0, animate); break;
    case "panRight": void c.truck(d * 0.1, 0, animate); break;
    case "panUp": void c.forward(d * 0.1, animate); break;
    case "panDown": void c.forward(-d * 0.1, animate); break;
  }
}
