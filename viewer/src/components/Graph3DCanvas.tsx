import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import {
  AdditiveBlending,
  BufferAttribute,
  BufferGeometry,
  Color,
  DynamicDrawUsage,
  Float32BufferAttribute,
  FogExp2,
  InstancedBufferAttribute,
  InstancedBufferGeometry,
  InstancedMesh,
  LineSegments,
  MOUSE,
  Matrix4,
  Mesh,
  MeshBasicMaterial,
  PerspectiveCamera,
  Plane,
  Quaternion,
  Scene,
  ShaderMaterial,
  Sphere,
  SphereGeometry,
  TOUCH,
  Vector2,
  Vector3,
  WebGLRenderer,
} from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { OutputPass } from "three/examples/jsm/postprocessing/OutputPass.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";
import { nodeColor } from "../lib/colors";
import { MotionField, layoutSeed, mulberry32, radiusForCount, revealOrder, type LayoutLink } from "../lib/layout";
import type { ColorBy, GraphCanvasHandle, GraphNode, GraphPayload, GraphSettings } from "../types";

interface Props {
  data: GraphPayload;
  visibleIds: Set<string>;
  selectedId: string | null;
  colorBy: ColorBy;
  showColors: boolean;
  settings: GraphSettings;
  /** 무대 배경만 테마를 탄다. 발광 렌더러 자체는 두 테마 모두 어두운 무대를 전제로 한다. */
  theme: "light" | "dark";
  onSelect: (id: string | null) => void;
}

/** One real, unique, undirected link. No synthetic node or edge ever exists. */
interface LinkState {
  key: string;
  s: number;
  t: number;
  /** clock ms at which both endpoints existed and the latent bond began to pull. */
  bondAt: number;
  /** clock ms at which the pair had come close enough for the line to be drawn. */
  readyAt: number;
  drawMs: number;
  /** endpoint separation, in simulation units, at the instant the line appeared. */
  spanAtReveal: number;
  /** closest these two have ever been, in simulation units. */
  minSpan: number;
  /** true when the edge draws from `t` toward `s` (the endpoint that settled later). */
  reversed: boolean;
}

interface Engine {
  sync: (data: GraphPayload) => void;
  setBackdrop: (color: string) => void;
  zoomBy: (factor: number) => void;
  fit: () => void;
  replay: () => void;
  markDirty: () => void;
  dispose: () => void;
}

/* --------------------------------------------------------------- formation
 * Nothing here is a destination. These numbers only decide *when* the next
 * handful of nodes is lit outside the cluster; where they end up is the
 * field's business. Nodes arrive in micro-cohorts of one to five, and a larger
 * cohort buys a proportionally longer pause, so the average rate never rises
 * and the build never reads as a burst. Nothing here is urgent: a few hundred
 * nodes take many minutes, and that is the point. */
const COHORT_WEIGHTS = [0.44, 0.26, 0.15, 0.09, 0.06];  // sizes 1..5
const FORMATION_INTERVAL_MS = 2400;   // per node, before the cohort multiplier
const FORMATION_BUDGET_MS = 900_000;  // only very large graphs ever compress
const FORMATION_MIN_MS = 420;
const COHORT_PAUSE = 0.8;             // wait = interval * size * this
const ARRIVAL_INTERVAL_MS = 2600;     // ingest / filter reveals use the same slow lane
const DRAG_LOOSEN = 0.7;              // a released node re-earns its own settle score

/* ------------------------------------------------------------------ physics */
const SIM_HZ = 60;
const SIM_DT = 1 / SIM_HZ;
const MAX_SUBSTEPS = 8;               // hard ceiling: a stalled tab cannot catch up
const FRAME_CLAMP_MS = 50;            // a long frame is dropped, never replayed
const RELAX_STEPS = 2600;             // reduced motion: settle the whole field at once

/* -------------------------------------------------------------- appearance */
const FADE_IN_MS = 4200;              // a new node swells out of nothing, slowly
const FILTER_FADE_MS = 320;
const COLOR_FLOOR = 0.62;             // how far ignition alone can light a traveller
const EDGE_MIN_NODES = 6;             // connections wait until there is a cluster
const EDGE_GATE = 0.62;               // both endpoints must be this integrated
/* A connection pulls before it is drawn. The moment both of its endpoints
 * exist, a latent bond fades in over `LATENT_RAMP_MS` and reaches
 * `LATENT_SHARE` of full strength — that is what reaches out and draws a
 * newcomer into the crowd it belongs to. Only once the pair has actually come
 * close does the line appear, and the remaining strength arrives with it. Doing
 * it the other way round is what produced long lines flung across the cluster
 * between nodes that had never approached each other. */
const LATENT_RAMP_MS = 30_000;
/* A pair that still has not managed to get close keeps reaching harder. This is
 * what eventually brings stubborn, far-apart neighbours together instead of
 * giving up and drawing a line across the gap: the bond creeps toward
 * `LATENT_MAX` over `LATENT_CREEP_MS` for as long as it stays unwired. */
const LATENT_CREEP_MS = 240_000;
const LATENT_MAX = 0.30;
/* Every node reaches hard for exactly one partner at a time: whichever of its
 * unmade connections has been waiting longest. That single link is allowed to
 * pull at full strength, so the two are actually brought together and collide,
 * and the node then moves on to the next one it owes.
 *
 * Doing this for *all* pending links instead is what stalls the wiring: with a
 * mean degree of nineteen, every node would be hauled nineteen ways at once,
 * the forces would cancel, nothing would move and the graph would seize around
 * half connected. One at a time is what makes full coverage reachable while
 * still requiring every single connection to be an actual collision. */
const REACH_MAX = 1.0;
const REACH_RAMP_MS = 45_000;
/* A handful of connections are between two heavy hubs, each buried in its own
 * crowd, and at ordinary strength they never quite manage to touch. A reach
 * that has gone unanswered keeps escalating, so the last few always finish. */
const REACH_ESCALATE = 1.6;
const REACH_PATIENCE_MS = 480_000;
/* A reach, and only a reach. This is the single most consequential number in
 * the whole engine: with a mean degree of nineteen, a latent bond anywhere near
 * full strength means every node is held by nineteen forces from every
 * direction at once, from the moment it appears. They cancel, the net force is
 * nothing, and the network is rigid before a single line has been drawn — which
 * is why no amount of current could move it, and why dragging a node left its
 * neighbours behind: one link was a nineteenth of what was already holding
 * them. Keep it faint. The graph then starts loose and free, and stiffens only
 * as connections are actually made. */
const LATENT_SHARE = 0.14;
/* Wiring happens *inside* the crowd, and it happens from the middle outward.
 * A pair may only be shown once both of its nodes have been drawn well within
 * the cluster (`WIRE_DEPTH`) and have come genuinely close to each other
 * (`REVEAL_MAX_SPAN`, a fraction of the containment radius). Among everything
 * that qualifies, the one drawn next is the one that is both closest together
 * and nearest the middle — so the wiring starts at the dense core and spreads
 * out, instead of nodes still on the rim flinging lines across the ball. */
/* A connection is drawn at the moment two nodes have *met and are easing back
 * apart*, which is what it looks like when something links up rather than being
 * wired across a gap. Concretely: the pair must have been within `TOUCH_FACTOR`
 * of their own contact floor at some point — that floor is theirs, sized by how
 * big each of them is drawn — and must now be a little wider than their closest
 * approach, but no wider than `REVEAL_STRETCH` times it. Before they have met,
 * nothing is drawn between them at all. */
/* Exactly 1: they have to have been inside each other's contact distance, not
 * near it. There is no discretisation risk in setting this to 1 — the collision
 * zone is an interval, from the contact distance down to zero, not a single
 * value to be stepped over, and a step is capped at STEP_CAP (0.008) against a
 * contact distance of 0.13 upward. A pair that gets pushed back out has, by
 * definition, been inside; that push is the collision. */
const TOUCH_FACTOR = 1.0;
/* Width of the ease-back window, in multiples of the pair's own meeting
 * distance. It has to be *added* to their closest approach, not multiplied by
 * it: a pair that ended up practically on top of each other has a minSpan of a
 * few thousandths, and a multiplicative window there is a few thousandths wide
 * — they pass straight through it and the connection never fires however many
 * times they meet. */
const REVEAL_STRETCH = 0.7;
/* The field stirs for as long as the graph is still finding itself, and what
 * decides that is how much of it is still unconnected — not a timer. It eases
 * off only as the last connections are made, and never stops completely. */
/* A connection is permanent on screen, but its grip is not. Once the line has
 * finished drawing, the pull behind it relaxes over `BOND_RELAX_MS` down to
 * `BOND_RESIDUAL`. That is what keeps the cluster from freezing solid as it
 * wires up: with a mean degree of nineteen, holding every finished connection
 * at full strength would lock every node in place long before it had met most
 * of its neighbours, and the wiring would stall around half done. Letting the
 * grip fade means groups form, loosen, drift on and run into new partners, and
 * the graph keeps working through its connections instead of seizing. */
const BOND_RELAX_MS = 60_000;
const BOND_RESIDUAL = 0.18;

const CALM_FROM = 0.9995;             // no easing off until essentially everything is wired
const CALM_FLOOR = 0.2;
/* How much harder a link pulls while the viewer is hauling on one of its ends. */
const DRAG_TENSION = 4.0;
const EASE_BACK = 1.02;               // how far past closest counts as easing apart
/* Anywhere inside the ball will do. This used to exclude the outer shell, back
 * when a line could appear between nodes that had merely come near each other —
 * but a connection now requires an actual collision, which is a far stronger
 * guarantee, and the depth test was only stranding the last few stragglers: the
 * least connected nodes are the ones that live out on the rim, and they were
 * the ones forbidden from finishing. */
const WIRE_DEPTH = 1.05;
const WIRE_CORE_BIAS = 0.75;
/* Some neighbours simply cannot all be close at once — with a mean degree of
 * nineteen the geometry does not allow it. Rather than hide those connections
 * for ever, a pair that has been reaching at each other this long is allowed to
 * be shown at a longer span. Each link runs this on its own bond clock, so it
 * is a permission that widens with patience, never a schedule of positions. */
/* There is deliberately no allowance for a pair that never manages to meet.
 * Every version of that idea — widen the test with patience, forgive a long
 * wait — ends up doing the one thing a connection must never do: appear between
 * two nodes that have not touched. If a pair cannot reach each other, its line
 * simply is not drawn yet; what brings them together is the bond creeping
 * stronger in the field, not a concession in the renderer. */
const EDGE_DROP = 0.28;               // below this a disturbed endpoint dims its edges
const EDGE_BUDGET_MS = 360_000;       // the whole wiring reveals over ~6 minutes
const EDGE_REVEAL_MIN_MS = 60;        // ...but never faster than one per this
const EDGE_REVEAL_MAX_MS = 1400;
/* A connection is an event, not a process. The two nodes hit each other and the
 * line is there — drawn quickly enough to read as a consequence of the impact
 * rather than as something creeping out on its own. The same progress still
 * drives the spring, so the tension arrives with the line. */
const EDGE_DRAW_MIN_MS = 900;
const EDGE_DRAW_MAX_MS = 1800;
const EDGE_ALPHA_FRACTION = 0.35;
/* Barely-there per-node shimmer. The real wandering is the current inside the
 * field, which carries neighbours together; this is only a whisper of
 * individual life on top, and it is kept far below the distance at which two
 * nodes read as touching so it can never pull a joined pair apart. */
const DRIFT_AMPLITUDE = 0.010;
const DRIFT_HZ = 0.021;               // ~48 s base period
const SCALE_TAU_MS = 220;
const CLICK_SLOP = 5;

/* ---------------------------------------------------------------- geometry */
const CORE_RADIUS = 0.0192;           // fraction of world scale — deliberately small
/* Size follows the connections a node has actually made, not the ones it might
 * one day have — the same signal brightness uses. A node therefore grows as it
 * joins the graph. `CORE_DEGREE_BASE` is how much of its size it has before it
 * has connected anything, so an unattached node is still comfortably visible. */
const CORE_DEGREE_BASE = 0.75;
const CORE_DEGREE_SPAN = 1.9;         // how much bigger a well connected node is
const HALO_BASE = 1.5;
const HALO_SPAN = 2.8;                // a hub's aura is much wider than a leaf's
const CAMERA_PULLBACK = 1.7;          // the ball fills the view, and never moves

/* -------------------------------------------------------------------- light
 * Nodes and edges are emitters, not painted dots. The core is drawn far above
 * white so the bloom pass has something to bleed, the halo carries the colour
 * out around it, and the whole frame goes through a real bloom rather than a
 * CSS shadow — which is the difference between a coloured dot and a star. */
/* Brightness carries the degree, hue carries the group. A well connected node
 * is up to `CORE_HUB` times brighter than a lonely one, but it is never washed
 * toward white — `CORE_WHITEN` is deliberately tiny — because the moment a core
 * saturates its group colour is gone. The halo is where most of the colour
 * lives, and the bloom threshold sits high enough that only the genuinely
 * bright hubs bleed light, leaving everything else its own hue. */
/* What reads as space is contrast, not brightness. Scaling everything down
 * together lowers the light but flattens the picture into grey dust; a
 * starfield is a very dark ground, most points modest, and a few that genuinely
 * burn. So the floor comes down and the span goes up while the overall level
 * stays low — but the floor stays high enough that an unconnected node is still
 * clearly a node and not an invisible one. */
const CORE_GAIN = 0.48;               // core brightness, in HDR units
const CORE_FLOOR = 0.42;              // an unconnected node: dim, but plainly there
const CORE_HUB = 5.2;                 // ...the most connected one genuinely burns
const CORE_WHITEN = 0.10;             // how far a lit core drives toward white
const HALO_GAIN = 0.60;
const EDGE_GAIN = 1.9;
/* Bloom radius is the one number that decides whether this looks like a field
 * of stars or like fog on a lens. Wide radii smear every lit pixel across a
 * large fraction of the screen, and the sum of that smear is a haze that buries
 * the edges. Keep it tight: a short bleed right around each bright core. */
const BLOOM_STRENGTH = 0.38;
const BLOOM_RADIUS = 0.13;
const BLOOM_THRESHOLD = 0.62;         // dim nodes stay matte; the bright ones bleed

/* ------------------------------------------------------------------ colors */
const BACKDROP = 0x0a0d13;
/** The unlit state. A node leaves it gradually from the moment it is created. */
const NEUTRAL_COLOR = new Color("#6f7787");
const DIM_COLOR = new Color("#1e2634");
const EDGE_BASE = new Color("#7ea8ff");
const EDGE_FOCUS = new Color("#cbe6ff");
const WHITE = new Color(1, 1, 1);

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
const smooth = (value: number) => {
  const t = clamp(value, 0, 1);
  return t * t * (3 - 2 * t);
};
/** Zero velocity at both ends: lines grow in and stop, they never overshoot. */
const smoother = (value: number) => {
  const t = clamp(value, 0, 1);
  return t * t * t * (t * (t * 6 - 15) + 10);
};
const spanProgress = (now: number, start: number, span: number) => {
  if (!Number.isFinite(start)) return 0;
  if (span <= 0) return now >= start ? 1 : 0;
  return clamp((now - start) / span, 0, 1);
};
const edgeKey = (a: string, b: string) => (a < b ? `${a} ${b}` : `${b} ${a}`);

/** Lifetime counters for the QA probe only — never surfaced in the UI. */
let engineGeneration = 0;

const HALO_VERTEX = `
attribute vec3 aOffset;
attribute float aSize;
attribute vec3 aColor;
attribute float aAlpha;
varying vec2 vQuad;
varying vec3 vColor;
varying float vAlpha;
void main() {
  vQuad = position.xy;
  vColor = aColor;
  vAlpha = aAlpha;
  vec4 mv = modelViewMatrix * vec4(aOffset, 1.0);
  mv.xy += position.xy * aSize;
  gl_Position = projectionMatrix * mv;
}`;

/**
 * Three lobes of radial falloff in the node's own colour: a wide faint corona,
 * a mid glow, and a tight hot centre. That stack is what reads as a star rather
 * than a translucent ball, and the tight lobe is what the bloom pass catches.
 */
const HALO_FRAGMENT = `
precision mediump float;
varying vec2 vQuad;
varying vec3 vColor;
varying float vAlpha;
void main() {
  float d = length(vQuad);
  if (d >= 1.0 || vAlpha <= 0.0) discard;
  float edge = 1.0 - d;
  float a = (pow(edge, 5.0) * 0.30 + pow(edge, 15.0) * 1.15) * vAlpha;
  if (a <= 0.002) discard;
  gl_FragColor = vec4(vColor, a);
}`;

const EDGE_VERTEX = `
attribute vec3 aColor;
attribute float aAlpha;
varying vec3 vColor;
varying float vAlpha;
void main() {
  vColor = aColor;
  vAlpha = aAlpha;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}`;

const EDGE_FRAGMENT = `
precision mediump float;
varying vec3 vColor;
varying float vAlpha;
void main() {
  if (vAlpha <= 0.002) discard;
  gl_FragColor = vec4(vColor, vAlpha);
}`;

const Graph3DCanvas = forwardRef<GraphCanvasHandle, Props>(function Graph3DCanvas(
  {data, visibleIds, selectedId, colorBy, showColors, settings, theme, onSelect},
  ref,
) {
  const host = useRef<HTMLDivElement>(null);
  const engineRef = useRef<Engine | null>(null);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;
  const propsRef = useRef({visibleIds, selectedId, colorBy, showColors});
  propsRef.current = {visibleIds, selectedId, colorBy, showColors};
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const dataRef = useRef(data);
  dataRef.current = data;

  /* The engine is built once and lives across every data update. An ingest or
   * an HMR reload calls `sync`, which keeps every existing node exactly where
   * it is and only introduces what is genuinely new. */
  useEffect(() => {
    const hostEl = host.current;
    if (!hostEl) return;

    engineGeneration += 1;
    const generation = engineGeneration;
    let replayCount = 0;

    /* ------------------------------------------------------------ identity */
    const seed = layoutSeed(dataRef.current.nodes.map((node) => node.id));
    const field = new MotionField(seed);
    const drawRandom = mulberry32(seed ^ 0x517cc1b7);
    const slotOf = new Map<string, number>();
    let slotNode: (GraphNode | null)[] = [];
    let freeSlots: number[] = [];
    let links: LinkState[] = [];
    const edgeMemory = new Map<string, {bondAt: number; readyAt: number; drawMs: number; reversed: boolean; spanAtReveal: number}>();
    let order: Int32Array<ArrayBufferLike> = new Int32Array(0);

    /* -------------------------------------------------------- slot buffers */
    let capacity = 0;
    let pos = new Float32Array(0);
    let baseRadius = new Float32Array(0);
    let drawnRadius = new Float32Array(0);
    let alpha = new Float32Array(0);
    let sizeNorm = new Float32Array(0);
    /** Connections this node has actually made, 0..1, rising as each line draws. */
    let litNorm = new Float32Array(0);
    /** Smoothed live maximum of the wired count, so brightness is a ranking. */
    let litBrightest = 1;
    /** For each node, the index of the unmade connection it is reaching for. */
    let reachOf = new Int32Array(0);
    /* Joined groups, over the connections that have actually been drawn. A node
     * is hauled in by everything its partner is already attached to, not by the
     * partner alone, so the group has to be known. Union-find, rebuilt whenever
     * a new line appears — which is rare enough that the cost is nothing. */
    let groupOf = new Int32Array(0);
    let groupSize = new Int32Array(0);
    let groupsDirty = true;
    let queued = new Uint8Array(0);
    let finalColors: Color[] = [];

    /* ------------------------------------------------------------ motion pref */
    const motionQuery = typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-reduced-motion: reduce)")
      : null;
    let reduced = motionQuery?.matches ?? false;

    /* -------------------------------------------------------------- timeline */
    let clock = 0;                    // ms of "graph time", scaled for QA runs
    let formation: number[] = [];     // the long build, one node at a time
    let arrivals: number[] = [];      // ingest and filter reveals, faster lane
    let formationInterval = FORMATION_INTERVAL_MS;
    let nextFormationAt = 0;
    let nextArrivalAt = 0;
    let edgeRevealInterval = EDGE_REVEAL_MAX_MS;
    let nextEdgeRevealAt = 0;
    let lastRevealAt = 0;

    /* ------------------------------------------------------------- rendering */
    const scene = new Scene();
    const backdrop = new Color(BACKDROP);
    scene.background = backdrop;
    const fog = new FogExp2(BACKDROP, 0.0005);
    scene.fog = fog;
    const camera = new PerspectiveCamera(52, 1, 0.4, 60_000);
    const renderer = new WebGLRenderer({antialias: true, alpha: true, powerPreference: "high-performance"});
    renderer.setClearAlpha(0);
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    const gl = renderer.domElement;
    gl.className = "graph-webgl";
    gl.style.cssText += ";position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none;outline:none";
    hostEl.appendChild(gl);

    const overlay = document.createElement("canvas");
    overlay.className = "graph-label-overlay";
    overlay.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none";
    hostEl.appendChild(overlay);
    const ctx = overlay.getContext("2d");

    /* Real bloom, not a drop-shadow. The composer runs in half-float, so the
     * cores can be brighter than white and bleed light into everything around
     * them — nodes read as stars and a lit edge reads as a filament rather than
     * a hairline. The background stays transparent so the page shows through. */
    const composer = new EffectComposer(renderer);
    const renderPass = new RenderPass(scene, camera);
    renderPass.clearAlpha = 0;
    composer.addPass(renderPass);
    const bloomPass = new UnrealBloomPass(new Vector2(1, 1), BLOOM_STRENGTH, BLOOM_RADIUS, BLOOM_THRESHOLD);
    composer.addPass(bloomPass);
    composer.addPass(new OutputPass());

    const controls = new OrbitControls(camera, gl);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.rotateSpeed = 0.5;
    controls.zoomSpeed = 1.1;
    controls.panSpeed = 0.6;
    controls.screenSpacePanning = true;
    controls.minDistance = 1.5;
    controls.mouseButtons = {LEFT: MOUSE.ROTATE, MIDDLE: MOUSE.DOLLY, RIGHT: MOUSE.PAN};
    controls.touches = {ONE: TOUCH.ROTATE, TWO: TOUCH.DOLLY_PAN};

    const sphere = new SphereGeometry(1, 12, 8);
    const coreMaterial = new MeshBasicMaterial({toneMapped: false});
    const haloMaterial = new ShaderMaterial({
      vertexShader: HALO_VERTEX,
      fragmentShader: HALO_FRAGMENT,
      transparent: true,
      depthWrite: false,
      blending: AdditiveBlending,
      toneMapped: false,
    });
    const edgeMaterial = new ShaderMaterial({
      vertexShader: EDGE_VERTEX,
      fragmentShader: EDGE_FRAGMENT,
      transparent: true,
      depthWrite: false,
      blending: AdditiveBlending,
      toneMapped: false,
    });

    let coreMesh: InstancedMesh | null = null;
    let haloGeometry: InstancedBufferGeometry | null = null;
    let haloOffset: InstancedBufferAttribute;
    let haloSize: InstancedBufferAttribute;
    let haloColor: InstancedBufferAttribute;
    let haloAlpha: InstancedBufferAttribute;
    let haloMesh: Mesh | null = null;

    /** GPU buffers follow the slot count; positions and state are never lost. */
    const ensureCapacity = (wanted: number) => {
      if (wanted <= capacity) return;
      field.ensureCapacity(wanted);
      const size = field.capacity;
      const grow = (source: Float32Array) => {
        const next = new Float32Array(size);
        next.set(source);
        return next;
      };
      const nextPos = new Float32Array(size * 3);
      nextPos.set(pos);
      pos = nextPos;
      baseRadius = grow(baseRadius);
      drawnRadius = grow(drawnRadius);
      alpha = grow(alpha);
      sizeNorm = grow(sizeNorm);
      litNorm = grow(litNorm);
      const nextReach = new Int32Array(size);
      nextReach.set(reachOf);
      reachOf = nextReach;
      groupOf = new Int32Array(size);
      groupSize = new Int32Array(size);
      groupsDirty = true;
      const nextQueued = new Uint8Array(size);
      nextQueued.set(queued);
      queued = nextQueued;
      while (finalColors.length < size) finalColors.push(new Color());
      while (slotNode.length < size) slotNode.push(null);
      capacity = size;

      if (coreMesh) { scene.remove(coreMesh); coreMesh.dispose(); }
      coreMesh = new InstancedMesh(sphere, coreMaterial, size);
      coreMesh.instanceMatrix.setUsage(DynamicDrawUsage);
      coreMesh.frustumCulled = false;
      coreMesh.count = 0;
      scene.add(coreMesh);

      if (haloMesh) { scene.remove(haloMesh); haloGeometry?.dispose(); }
      haloGeometry = new InstancedBufferGeometry();
      haloGeometry.setAttribute("position", new Float32BufferAttribute(
        [-1, -1, 0, 1, -1, 0, 1, 1, 0, -1, 1, 0], 3));
      haloGeometry.setIndex([0, 1, 2, 0, 2, 3]);
      haloOffset = new InstancedBufferAttribute(new Float32Array(size * 3), 3).setUsage(DynamicDrawUsage);
      haloSize = new InstancedBufferAttribute(new Float32Array(size), 1).setUsage(DynamicDrawUsage);
      haloColor = new InstancedBufferAttribute(new Float32Array(size * 3), 3).setUsage(DynamicDrawUsage);
      haloAlpha = new InstancedBufferAttribute(new Float32Array(size), 1).setUsage(DynamicDrawUsage);
      haloGeometry.setAttribute("aOffset", haloOffset);
      haloGeometry.setAttribute("aSize", haloSize);
      haloGeometry.setAttribute("aColor", haloColor);
      haloGeometry.setAttribute("aAlpha", haloAlpha);
      haloGeometry.boundingSphere = new Sphere(new Vector3(), Infinity);
      haloGeometry.instanceCount = 0;
      haloMesh = new Mesh(haloGeometry, haloMaterial);
      haloMesh.frustumCulled = false;
      haloMesh.renderOrder = 2;
      scene.add(haloMesh);
    };

    let edgeCapacity = 0;
    let edgeGeometry = new BufferGeometry();
    let edgePositions = new Float32Array(0);
    let edgeColors = new Float32Array(0);
    let edgeAlphas = new Float32Array(0);
    const edgeLines = new LineSegments(edgeGeometry, edgeMaterial);
    edgeLines.frustumCulled = false;
    edgeLines.renderOrder = 1;
    scene.add(edgeLines);

    const ensureEdgeCapacity = (wanted: number) => {
      if (wanted <= edgeCapacity) return;
      const size = Math.max(256, 1 << Math.ceil(Math.log2(wanted + 1)));
      edgeGeometry.dispose();
      edgeGeometry = new BufferGeometry();
      edgePositions = new Float32Array(size * 6);
      edgeColors = new Float32Array(size * 6);
      edgeAlphas = new Float32Array(size * 2);
      edgeGeometry.setAttribute("position", new BufferAttribute(edgePositions, 3).setUsage(DynamicDrawUsage));
      edgeGeometry.setAttribute("aColor", new BufferAttribute(edgeColors, 3).setUsage(DynamicDrawUsage));
      edgeGeometry.setAttribute("aAlpha", new BufferAttribute(edgeAlphas, 1).setUsage(DynamicDrawUsage));
      edgeGeometry.setDrawRange(0, 0);
      edgeGeometry.boundingSphere = new Sphere(new Vector3(), Infinity);
      edgeLines.geometry = edgeGeometry;
      edgeCapacity = size;
    };

    /* ------------------------------------------------------------- topology */
    let neighborSets: Set<number>[] = [];
    let nodeCount = 0;

    /**
     * Fold a fresh payload into the living field. Slots are matched by node id,
     * so an ingest is additive: nobody who already exists is moved, rescheduled
     * or recoloured, and only genuinely new ids get a place in the outer cloud.
     */
    const sync = (payload: GraphPayload) => {
      const incoming = new Set(payload.nodes.map((node) => node.id));
      for (const [id, slot] of Array.from(slotOf)) {
        if (incoming.has(id)) continue;
        field.sleep(slot);
        slotOf.delete(id);
        slotNode[slot] = null;
        queued[slot] = 0;
        alpha[slot] = 0;
        freeSlots.push(slot);
      }
      const newcomers: number[] = [];
      for (const node of payload.nodes) {
        let slot = slotOf.get(node.id);
        if (slot === undefined) {
          slot = freeSlots.pop();
          if (slot === undefined) slot = nodeCount++;
          ensureCapacity(Math.max(nodeCount, slot + 1));
          slotOf.set(node.id, slot);
          queued[slot] = 0;
          alpha[slot] = 0;
          newcomers.push(slot);
        }
        slotNode[slot] = node;
      }
      nodeCount = Math.max(nodeCount, slotOf.size);
      ensureCapacity(Math.max(1, nodeCount));

      const layoutLinks: LayoutLink[] = [];
      const nextLinks: LinkState[] = [];
      const seen = new Set<string>();
      for (const edge of payload.edges) {
        const s = slotOf.get(edge.source);
        const t = slotOf.get(edge.target);
        if (s === undefined || t === undefined || s === t) continue;
        const key = edgeKey(edge.source, edge.target);
        if (seen.has(key)) continue;
        seen.add(key);
        const memory = edgeMemory.get(key);
        nextLinks.push({
          key,
          s,
          t,
          bondAt: memory?.bondAt ?? Infinity,
          readyAt: memory?.readyAt ?? Infinity,
          drawMs: memory?.drawMs ?? EDGE_DRAW_MIN_MS + drawRandom() * (EDGE_DRAW_MAX_MS - EDGE_DRAW_MIN_MS),
          reversed: memory?.reversed ?? false,
          spanAtReveal: memory?.spanAtReveal ?? 0,
          minSpan: Infinity,
        });
        layoutLinks.push({s, t});
      }
      for (const key of Array.from(edgeMemory.keys())) if (!seen.has(key)) edgeMemory.delete(key);
      links = nextLinks;
      ensureEdgeCapacity(Math.max(1, links.length));
      syncEdgePace();

      field.setTopology(capacity, layoutLinks);
      groupsDirty = true;
      neighborSets = Array.from({length: capacity}, () => new Set<number>());
      for (const link of links) {
        neighborSets[link.s].add(link.t);
        neighborSets[link.t].add(link.s);
      }
      order = revealOrder(capacity, layoutLinks);

      // p95 cap keeps a couple of mega-hubs from stretching the whole size scale
      const degrees: number[] = [];
      for (let slot = 0; slot < capacity; slot += 1) {
        if (slotNode[slot]) degrees.push(neighborSets[slot].size);
      }
      degrees.sort((a, b) => a - b);
      const degreeCap = Math.max(1, degrees[Math.floor(degrees.length * 0.95)] || 1);
      for (let slot = 0; slot < capacity; slot += 1) {
        sizeNorm[slot] = slotNode[slot]
          ? clamp(Math.log2(neighborSets[slot].size + 1) / Math.log2(degreeCap + 1), 0, 1)
          : 0;
      }
      refreshColors(true);
      syncRadii();

      /* An ingest wakes only the newcomers, and reheats only the pocket around
       * them. The rest of the globe keeps its colour and its place. */
      for (const slot of newcomers) {
        if (queued[slot] || field.awake[slot]) continue;
        if (!propsRef.current.visibleIds.has(slotNode[slot]!.id)) continue;
        queued[slot] = 1;
        arrivals.push(slot);
      }
      if (reduced && newcomers.length) settleImmediately();
      dirty = true;
    };

    /* ---------------------------------------------------------------- scale */
    const worldScaleTarget = () => clamp(settingsRef.current.linkDistance * 0.34, 30, 130);
    let worldScale = worldScaleTarget();
    /** Radius the finished globe will want. The camera frames the *result* from
     * the first frame and then holds: nothing here ever follows the live
     * extents, so the view cannot chase a growing cluster. */
    const expectedRadius = () =>
      radiusForCount(Math.max(1, slotOf.size, propsRef.current.visibleIds.size)) * worldScale;
    let ballRadius = expectedRadius();
    const syncRadii = () => {
      const scale = settingsRef.current.nodeScale;
      for (let slot = 0; slot < capacity; slot += 1) {
        baseRadius[slot] = worldScale * CORE_RADIUS * scale
          * (CORE_DEGREE_BASE + CORE_DEGREE_SPAN * (0.35 * sizeNorm[slot] + 0.65 * litNorm[slot]));
      }
    };

    /* --------------------------------------------------------------- colors */
    let colorKey = "";
    function refreshColors(force = false) {
      const current = propsRef.current;
      const key = `${current.colorBy}:${current.showColors}`;
      if (!force && key === colorKey) return;
      colorKey = key;
      const groups = dataRef.current.groups;
      for (let slot = 0; slot < capacity; slot += 1) {
        const node = slotNode[slot];
        if (node) finalColors[slot].set(nodeColor(node, current.colorBy, groups, current.showColors));
      }
    }

    /* ------------------------------------------------------------- formation */
    const visibleSlots = () => {
      const ids = propsRef.current.visibleIds;
      const list: number[] = [];
      for (let rank = 0; rank < order.length; rank += 1) {
        const slot = order[rank];
        const node = slotNode[slot];
        if (node && ids.has(node.id)) list.push(slot);
      }
      return list;
    };

    /** Reveal pace for the wiring: the whole graph's links spread over the
     * edge budget, floored so a small graph still connects gently. */
    const syncEdgePace = () => {
      edgeRevealInterval = links.length > 0
        ? clamp(EDGE_BUDGET_MS / links.length, EDGE_REVEAL_MIN_MS, EDGE_REVEAL_MAX_MS)
        : EDGE_REVEAL_MAX_MS;
    };

    /** Replay: forget everything and grow the current filtered graph again. */
    const replay = () => {
      replayCount += 1;
      field.reset();
      alpha.fill(0);
      queued.fill(0);
      for (const link of links) {
        link.bondAt = Infinity; link.readyAt = Infinity; link.reversed = false; link.minSpan = Infinity;
      }
      groupsDirty = true;
      edgeMemory.clear();
      returningLoosen = -1;
      formation = visibleSlots();
      arrivals = [];
      lastRevealAt = clock;
      for (const slot of formation) queued[slot] = 1;
      formationInterval = formation.length > 1
        ? clamp(FORMATION_BUDGET_MS / formation.length, FORMATION_MIN_MS, FORMATION_INTERVAL_MS)
        : FORMATION_INTERVAL_MS;
      syncEdgePace();
      nextFormationAt = clock;
      nextArrivalAt = clock;
      nextEdgeRevealAt = clock;
      if (reduced) settleImmediately();
      dirty = true;
    };

    /** Reduced motion: no build, no travel — one stable picture, right now. */
    function settleImmediately() {
      for (const slot of visibleSlots()) {
        queued[slot] = 1;
        if (!field.awake[slot]) field.wake(slot);
      }
      formation = [];
      arrivals = [];
      field.relax(RELAX_STEPS, SIM_DT, tuningOf());
      for (let slot = 0; slot < capacity; slot += 1) {
        alpha[slot] = field.awake[slot] && propsRef.current.visibleIds.has(slotNode[slot]?.id ?? "") ? 1 : 0;
      }
      for (const link of links) {
        const ready = field.awake[link.s] && field.awake[link.t];
        link.bondAt = ready ? -1e9 : Infinity;
        link.readyAt = ready ? -1e9 : Infinity;
        if (!ready) continue;
        link.spanAtReveal = Math.hypot(
          field.px[link.t] - field.px[link.s],
          field.py[link.t] - field.py[link.s],
          field.pz[link.t] - field.pz[link.s],
        );
        edgeMemory.set(link.key, {
          bondAt: link.bondAt, readyAt: link.readyAt, drawMs: link.drawMs,
          reversed: link.reversed, spanAtReveal: link.spanAtReveal,
        });
      }
      dirty = true;
    }

    const tuningOf = () => ({
      repel: settingsRef.current.repelForce,
      linkStrength: settingsRef.current.linkStrength,
      center: settingsRef.current.centerForce,
    });

    /** A filter that reveals sleeping nodes queues them — it never reshuffles the globe. */
    let scannedVisible: Set<string> | null = null;
    const scanVisible = () => {
      const ids = propsRef.current.visibleIds;
      if (ids === scannedVisible) return;
      scannedVisible = ids;
      for (let rank = 0; rank < order.length; rank += 1) {
        const slot = order[rank];
        const node = slotNode[slot];
        if (!node || queued[slot] || field.awake[slot]) continue;
        if (!ids.has(node.id)) continue;
        queued[slot] = 1;
        arrivals.push(slot);
      }
      if (reduced && arrivals.length) settleImmediately();
    };

    /** Recompute the joined groups and publish how much each one can haul. */
    const rebuildGroups = () => {
      groupsDirty = false;
      for (let slot = 0; slot < capacity; slot += 1) { groupOf[slot] = slot; groupSize[slot] = 1; }
      const find = (node: number) => {
        let root = node;
        while (groupOf[root] !== root) root = groupOf[root];
        while (groupOf[node] !== root) { const next = groupOf[node]; groupOf[node] = root; node = next; }
        return root;
      };
      for (const link of links) {
        if (!Number.isFinite(link.readyAt)) continue;
        const a = find(link.s);
        const b = find(link.t);
        if (a === b) continue;
        if (groupSize[a] >= groupSize[b]) { groupOf[b] = a; groupSize[a] += groupSize[b]; }
        else { groupOf[a] = b; groupSize[b] += groupSize[a]; }
      }
      let biggest = 1;
      for (let slot = 0; slot < capacity; slot += 1) {
        if (groupOf[slot] === slot && groupSize[slot] > biggest) biggest = groupSize[slot];
      }
      const scale = 1 / Math.log2(biggest + 1);
      for (let slot = 0; slot < capacity; slot += 1) {
        field.groupMass[slot] = slotNode[slot]
          ? clamp(Math.log2(groupSize[find(slot)] + 1) * scale, 0, 1)
          : 0;
      }
    };

    /** Micro-cohort size: usually one, occasionally up to five, never more. */
    const cohortSize = (available: number) => {
      let roll = drawRandom();
      for (let size = 0; size < COHORT_WEIGHTS.length; size += 1) {
        roll -= COHORT_WEIGHTS[size];
        if (roll <= 0) return Math.min(available, size + 1);
      }
      return Math.min(available, 1);
    };

    /**
     * Light the next micro-cohort. A bigger cohort costs a proportionally
     * longer pause, so the mean arrival rate is the same whatever the dice
     * say — the graph can gather in twos and threes but never in a burst.
     * Waking never reheats anybody: the newcomer's energy is its own.
     */
    const pumpQueues = () => {
      if (arrivals.length && clock >= nextArrivalAt) {
        const size = cohortSize(arrivals.length);
        for (let taken = 0; taken < size; taken += 1) field.wake(arrivals.shift()!);
        nextArrivalAt = clock + ARRIVAL_INTERVAL_MS * size * COHORT_PAUSE;
        return true;
      }
      if (formation.length && clock >= nextFormationAt) {
        const size = cohortSize(formation.length);
        for (let taken = 0; taken < size; taken += 1) field.wake(formation.shift()!);
        nextFormationAt = clock + formationInterval * size * COHORT_PAUSE;
        return true;
      }
      return false;
    };

    /* --------------------------------------------------------- drag / release */
    let draggingSlot = -1;
    let dragMoved = false;
    let dragPointer = -1;
    /** True when the browser handed us the pointer, so a still hold is safe. */
    let dragCaptured = false;
    let returningLoosen = -1;
    const dragOrigin = new Vector3();
    const dragPlane = new Plane();

    /* ------------------------------------------------------------------ state */
    const matrix = new Matrix4();
    const vecA = new Vector3();
    const vecB = new Vector3();
    const scaleVec = new Vector3();
    const rotation = new Quaternion();
    const scratch = new Color();
    const mixColor = new Color();
    let hovered = -1;
    let dirty = true;
    let needsRender = true;
    let disposed = false;
    let lastFrameAt = performance.now();
    let accumulator = 0;

    /**
     * Publish this frame's link gains to the field. A link's spring weight *is*
     * its reveal progress: nothing pulls before its line starts to draw, and a
     * line half drawn is a spring at half tension. Written before the physics
     * runs so every substep this frame sees the same, current value.
     */
    const publishLinkGains = () => {
      const gains = field.linkGain;
      litNorm.fill(0);
      /* Who each node is currently reaching for: its oldest unmade connection. */
      if (groupsDirty) rebuildGroups();
      reachOf.fill(-1);
      field.reachPartner.fill(-1);
      for (let index = 0; index < links.length; index += 1) {
        const link = links[index];
        if (Number.isFinite(link.readyAt)) continue;
        if (!Number.isFinite(link.bondAt)) continue;
        if (!field.awake[link.s] || !field.awake[link.t]) continue;
        const s = reachOf[link.s];
        const t = reachOf[link.t];
        if (s < 0 || link.bondAt < links[s].bondAt) reachOf[link.s] = index;
        if (t < 0 || link.bondAt < links[t].bondAt) reachOf[link.t] = index;
      }
      for (let index = 0; index < links.length; index += 1) {
        const link = links[index];
        if (reduced) {
          const on = Number.isFinite(link.bondAt) ? 1 : 0;
          gains[index] = on;
          field.linkShown[index] = on;
          continue;
        }
        // the bond reaches out first, the drawn line finishes the job
        /* Reaching means: this is the connection one of its ends is currently
         * working on — or it runs to the same joined group that end is working
         * on, in which case the whole group hauls together instead of one node
         * tugging on its own.
         *
         * That second clause only means anything while the end is reaching
         * across a gap, so it is asked only of an end whose own group is not
         * already the group it is reaching into. Without that condition the
         * clause quietly inverts as the graph finishes: once everything is one
         * group, every link of a node that still owes a connection answers
         * "same group" and is flagged reaching — so all hundred-odd of them
         * pull with the group multiplier, at the raised ceiling, and skip the
         * hub softening entirely. The one connection the node is actually
         * working on is then buried under its own neighbourhood hauling it in
         * every direction at once, and the last pair never closes. */
        let reaching = reachOf[link.s] === index || reachOf[link.t] === index;
        if (!reaching) {
          const viaS = reachOf[link.s];
          const viaT = reachOf[link.t];
          if (viaS >= 0) {
            const partner = links[viaS].s === link.s ? links[viaS].t : links[viaS].s;
            if (groupOf[link.s] !== groupOf[partner] && groupOf[partner] === groupOf[link.t]) reaching = true;
          }
          if (!reaching && viaT >= 0) {
            const partner = links[viaT].s === link.t ? links[viaT].t : links[viaT].s;
            if (groupOf[link.t] !== groupOf[partner] && groupOf[partner] === groupOf[link.s]) reaching = true;
          }
        }
        field.linkReach[index] = reaching ? 1 : 0;
        // the squeeze belongs to the exact pair that is coming together
        if (reachOf[link.s] === index) field.reachPartner[link.s] = link.t;
        if (reachOf[link.t] === index) field.reachPartner[link.t] = link.s;
        const latent = LATENT_SHARE * smoother(spanProgress(clock, link.bondAt, LATENT_RAMP_MS))
          + (LATENT_MAX - LATENT_SHARE) * smoother(spanProgress(clock, link.bondAt, LATENT_CREEP_MS))
          + (reaching
            ? (REACH_MAX - LATENT_MAX) * smoother(spanProgress(clock, link.bondAt, REACH_RAMP_MS))
              + REACH_ESCALATE * smoother(spanProgress(clock, link.bondAt, REACH_PATIENCE_MS))
            : 0);
        const shown = smoother(spanProgress(clock, link.readyAt, link.drawMs));
        const settled = smoother(spanProgress(clock, link.readyAt + link.drawMs, BOND_RELAX_MS));
        const grip = shown * (1 - (1 - BOND_RESIDUAL) * settled);
        const held = draggingSlot >= 0 && (link.s === draggingSlot || link.t === draggingSlot);
        gains[index] = Math.max(latent, grip) * (held ? DRAG_TENSION : 1);
        field.linkShown[index] = shown;
        /* Light is earned by connecting, not by having connections to make. A
         * node brightens exactly as much as its lines have actually been drawn,
         * so a newcomer with a hundred waiting neighbours is still only a faint
         * point until it starts joining them. */
        if (shown > 0) { litNorm[link.s] += shown; litNorm[link.t] += shown; }
        if (Number.isFinite(link.readyAt)) continue;
        if (!field.awake[link.s] || !field.awake[link.t]) continue;
        const span = Math.hypot(
          field.px[link.t] - field.px[link.s],
          field.py[link.t] - field.py[link.s],
          field.pz[link.t] - field.pz[link.s],
        );
        // the field samples this every substep; keep the frame-level value in
        // step with it so a reveal decision never sees the coarser number
        if (field.linkMinSpan[index] < link.minSpan) link.minSpan = field.linkMinSpan[index];
        if (span < link.minSpan) link.minSpan = span;
      }
      /* Relative, not absolute. Normalising against a fixed degree cap lets a
       * whole crowd of well connected nodes sit at full brightness together;
       * normalising against the brightest node there actually is means exactly
       * one node is the brightest, and everything else is ranked below it. */
      let mostWired = 0;
      for (let slot = 0; slot < capacity; slot += 1) {
        if (litNorm[slot] > mostWired) mostWired = litNorm[slot];
      }
      litBrightest += (mostWired - litBrightest) * 0.02;
      const scale = 1 / Math.log2(Math.max(1, litBrightest) + 1);
      const bodyScale = settingsRef.current.nodeScale;
      for (let slot = 0; slot < capacity; slot += 1) {
        litNorm[slot] = clamp(Math.log2(litNorm[slot] + 1) * scale, 0, 1);
        /* Publish the radius this node will be drawn at, in simulation units,
         * with the identical formula `syncRadii` uses. The physics measures
         * contact against exactly the bodies on screen — change the size rule
         * and contact follows it automatically instead of silently drifting. */
        field.bodyRadius[slot] = CORE_RADIUS * bodyScale
          * (CORE_DEGREE_BASE + CORE_DEGREE_SPAN * (0.35 * sizeNorm[slot] + 0.65 * litNorm[slot]));
      }
    };

    /* ---------------------------------------------------------------- physics */
    const advance = (deltaMs: number) => {
      const tuning = tuningOf();
      publishLinkGains();
      if (!reduced) {
        /* Fixed timestep, bounded work, no backlog. `deltaMs` is already
         * clamped upstream, so in production this is three substeps at most;
         * the ceiling only widens for a deliberately accelerated QA clock, and
         * whatever is left over is discarded rather than replayed. */
        const maxSteps = Math.min(240, Math.max(MAX_SUBSTEPS, Math.ceil(deltaMs / 1000 / SIM_DT) + 1));
        accumulator += deltaMs / 1000;
        let steps = 0;
        while (accumulator >= SIM_DT && steps < maxSteps) {
          field.step(SIM_DT, tuning);
          accumulator -= SIM_DT;
          steps += 1;
        }
        if (accumulator > SIM_DT * 2) accumulator = SIM_DT * 2;
      }

      // world scale eases so "link distance" is a pure zoom of the same field
      const wantedScale = worldScaleTarget();
      if (Math.abs(wantedScale - worldScale) > 1e-3) {
        const blend = reduced ? 1 : 1 - Math.exp(-deltaMs / SCALE_TAU_MS);
        worldScale += (wantedScale - worldScale) * blend;
        if (Math.abs(wantedScale - worldScale) < 0.02) worldScale = wantedScale;
      }
      syncRadii();

      const ids = propsRef.current.visibleIds;
      const fadeBlend = reduced ? 1 : 1 - Math.exp(-deltaMs / (FILTER_FADE_MS / 3));
      const wakeBlend = reduced ? 1 : 1 - Math.exp(-deltaMs / (FADE_IN_MS / 3));
      const driftPhase = (clock / 1000) * DRIFT_HZ * Math.PI * 2;
      const driftAmp = worldScale * DRIFT_AMPLITUDE;
      let live = 0;
      let busy = false;

      for (let slot = 0; slot < capacity; slot += 1) {
        const node = slotNode[slot];
        const awake = field.awake[slot] === 1;
        const wants = awake && node !== null && ids.has(node.id) ? 1 : 0;
        if (Math.abs(wants - alpha[slot]) > 1e-3) {
          alpha[slot] += (wants - alpha[slot]) * (wants > alpha[slot] ? wakeBlend : fadeBlend);
          if (Math.abs(wants - alpha[slot]) < 0.004) alpha[slot] = wants;
          busy = true;
        }
        if (!awake) continue;

        const at = slot * 3;
        const fx = field.px[slot];
        const fy = field.py[slot];
        const fz = field.pz[slot];
        live += 1;

        /* Settled nodes are never locked: a barely-there low-frequency float,
         * applied only at draw time so it can never feed back into the field. */
        const rest = field.settle[slot];
        let ox = 0;
        let oy = 0;
        let oz = 0;
        if (rest > 0.02 && !reduced && slot !== draggingSlot) {
          const p = field.phase[slot];
          const amp = driftAmp * rest;
          ox = Math.sin(driftPhase + p) * amp;
          oy = Math.sin(driftPhase * 0.83 + p * 1.7) * amp;
          oz = Math.sin(driftPhase * 1.19 + p * 2.3) * amp;
        }
        pos[at] = fx * worldScale + ox;
        pos[at + 1] = fy * worldScale + oy;
        pos[at + 2] = fz * worldScale + oz;
        if (field.speed[slot] > 0.01 || rest < 0.999) busy = true;
      }

      /* Depth cues follow the *expected* globe, which only moves when the world
       * scale does. Nothing here reads the live extents, so the view never
       * chases the cluster as it grows. */
      /* Stir at full strength for as long as there is real wiring left to do,
       * and begin to settle only over the last stretch of it. Easing in
       * proportion to progress means the cluster is already half-asleep while
       * most of the graph is still looking for its connections. */
      let wiredLinks = 0;
      for (const link of links) if (Number.isFinite(link.readyAt)) wiredLinks += 1;
      const done = links.length > 0 ? wiredLinks / links.length : 1;
      const finishing = smooth(clamp((done - CALM_FROM) / (1 - CALM_FROM), 0, 1));
      field.stir = reduced ? 0 : CALM_FLOOR + (1 - CALM_FLOOR) * (1 - finishing);

      const framing = expectedRadius();
      if (Math.abs(framing - ballRadius) > 0.01) {
        ballRadius = framing;
        fog.density = 0.17 / Math.max(1, ballRadius);
        controls.maxDistance = ballRadius * 120;
        camera.far = Math.max(2000, ballRadius * 400);
        camera.updateProjectionMatrix();
      }

      /* The latent bond starts the moment both endpoints exist. This is the
       * reach that pulls a newcomer toward the crowd it belongs to, long before
       * anything is drawn between them. */
      for (const link of links) {
        if (Number.isFinite(link.bondAt)) continue;
        if (!field.awake[link.s] || !field.awake[link.t]) continue;
        link.bondAt = clock;
        edgeMemory.set(link.key, {
          bondAt: link.bondAt, readyAt: link.readyAt, drawMs: link.drawMs,
          reversed: link.reversed, spanAtReveal: link.spanAtReveal,
        });
      }

      /* Wiring waits for a cluster to exist, then draws one line at a time on
       * its own slow clock — and always the *closest* pair still unwired, never
       * further apart than a fraction of the ball. A connection is therefore
       * something that appears once two nodes have come together, not a wire
       * flung between two nodes that never approached. */
      if (field.activeCount >= EDGE_MIN_NODES) {
        // never more backlog than this frame is worth: a hidden tab resumes at
        // the same pace it left, it does not catch up
        if (nextEdgeRevealAt < clock - deltaMs) nextEdgeRevealAt = clock - deltaMs;
        const ballRadiusNow = field.radius();
        const depth = ballRadiusNow * WIRE_DEPTH;
        while (clock >= nextEdgeRevealAt) {
          let chosen: LinkState | null = null;
          let best = Infinity;
          let chosenFromS = false;
          for (const link of links) {
            if (Number.isFinite(link.readyAt)) continue;
            if (!field.awake[link.s] || !field.awake[link.t]) continue;
            if (field.integration[link.s] < EDGE_GATE || field.integration[link.t] < EDGE_GATE) continue;
            const rs = Math.hypot(field.px[link.s], field.py[link.s], field.pz[link.s]);
            const rt = Math.hypot(field.px[link.t], field.py[link.t], field.pz[link.t]);
            if (rs > depth || rt > depth) continue;   // still on the rim: not yet
            const span = Math.hypot(
              field.px[link.t] - field.px[link.s],
              field.py[link.t] - field.py[link.s],
              field.pz[link.t] - field.pz[link.s],
            );
            // they have to have hit each other first — no exceptions, ever
            if (link.minSpan > field.meetingDistance(link.s, link.t) * TOUCH_FACTOR) continue;
            // (meeting distance is read once below, after the contact test passes)
            const meet = field.meetingDistance(link.s, link.t);
            if (span < link.minSpan * EASE_BACK) continue;              // still closing: wait
            if (span > link.minSpan + meet * REVEAL_STRETCH) continue;  // already drifted off
            const score = span + WIRE_CORE_BIAS * (rs + rt) * 0.5;
            if (score >= best) continue;
            best = score;
            chosen = link;
            chosenFromS = rs <= rt;
          }
          if (!chosen) { nextEdgeRevealAt = clock + edgeRevealInterval; break; }
          chosen.readyAt = clock;
          lastRevealAt = clock;
          groupsDirty = true;
          // the line grows from whichever end is deeper in, outward
          chosen.reversed = !chosenFromS;
          chosen.spanAtReveal = Math.hypot(
            field.px[chosen.t] - field.px[chosen.s],
            field.py[chosen.t] - field.py[chosen.s],
            field.pz[chosen.t] - field.pz[chosen.s],
          );
          edgeMemory.set(chosen.key, {
            bondAt: chosen.bondAt, readyAt: chosen.readyAt, drawMs: chosen.drawMs,
            reversed: chosen.reversed, spanAtReveal: chosen.spanAtReveal,
          });
          nextEdgeRevealAt += edgeRevealInterval;
          busy = true;
        }
      }

      if (returningLoosen >= 0) {
        field.loosen(returningLoosen, DRAG_LOOSEN);
        returningLoosen = -1;
      }
      return busy || formation.length > 0 || arrivals.length > 0;
    };

    /* -------------------------------------------------------------- painting */
    const paint = () => {
      refreshColors();
      const current = propsRef.current;
      const config = settingsRef.current;
      const focus = current.selectedId ? slotOf.get(current.selectedId) ?? -1 : hovered;
      const focusNeighbors = focus >= 0 ? neighborSets[focus] : null;
      let instances = 0;
      drawnRadius.fill(0);
      if (!coreMesh || !haloGeometry) return;

      for (let slot = 0; slot < capacity; slot += 1) {
        if (alpha[slot] <= 0.004) continue;
        const at = slot * 3;
        const rest = field.settle[slot];
        const grow = smooth(alpha[slot]);
        /* Light starts at creation. `ignite` is already climbing while the node
         * is still outside and travelling; settling only finishes the job. */
        const bloom = smooth(field.ignite[slot]) * (COLOR_FLOOR + (1 - COLOR_FLOOR) * rest);
        const isFocus = slot === focus;
        const near = focus < 0 || isFocus || Boolean(focusNeighbors?.has(slot));
        const radius = baseRadius[slot] * grow * (isFocus ? 1.5 : 1);
        drawnRadius[slot] = radius;

        vecA.set(pos[at], pos[at + 1], pos[at + 2]);
        scaleVec.setScalar(radius);
        matrix.compose(vecA, rotation, scaleVec);
        coreMesh.setMatrixAt(instances, matrix);

        // colour bleeds out of neutral from the moment the node exists
        scratch.copy(NEUTRAL_COLOR).lerp(finalColors[slot], bloom);
        if (!near) scratch.lerp(DIM_COLOR, 0.6);
        haloColor.array[instances * 3] = scratch.r * HALO_GAIN;
        haloColor.array[instances * 3 + 1] = scratch.g * HALO_GAIN;
        haloColor.array[instances * 3 + 2] = scratch.b * HALO_GAIN;

        /* The core is driven past white as the node lights up: the bloom pass
         * reads that overflow and turns the dot into a point of light. */
        const heat = CORE_GAIN * (CORE_FLOOR + CORE_HUB * litNorm[slot])
          * grow * (0.4 + 0.6 * smooth(field.ignite[slot])) * (near ? 1 : 0.35);
        mixColor.copy(scratch).lerp(WHITE, CORE_WHITEN * smooth(field.ignite[slot])).multiplyScalar(heat);
        coreMesh.setColorAt(instances, mixColor);

        haloOffset.array[instances * 3] = vecA.x;
        haloOffset.array[instances * 3 + 1] = vecA.y;
        haloOffset.array[instances * 3 + 2] = vecA.z;
        haloSize.array[instances] = radius * (HALO_BASE + litNorm[slot] * HALO_SPAN);
        const a = (0.012 + litNorm[slot] * 0.20) * grow * alpha[slot] * (isFocus ? 2.2 : 1)
          * (0.35 + 0.65 * smooth(field.ignite[slot]));
        haloAlpha.array[instances] = near ? a : a * 0.4;
        instances += 1;
      }

      coreMesh.count = instances;
      coreMesh.instanceMatrix.needsUpdate = true;
      if (coreMesh.instanceColor) coreMesh.instanceColor.needsUpdate = true;
      coreMesh.computeBoundingSphere();
      haloGeometry.instanceCount = instances;
      haloOffset.needsUpdate = true;
      haloSize.needsUpdate = true;
      haloColor.needsUpdate = true;
      haloAlpha.needsUpdate = true;

      // edges start and end on the core surface — never through the centre
      const thickness = clamp(config.linkThickness, 0.15, 2.5);
      let vertex = 0;
      for (const link of links) {
        const gate = Math.min(alpha[link.s], alpha[link.t]);
        if (gate <= 0.004) continue;
        const draw = spanProgress(clock, link.readyAt, reduced ? 0 : link.drawMs);
        if (draw <= 0) continue;
        const si = link.s * 3;
        const ti = link.t * 3;
        vecA.set(pos[si], pos[si + 1], pos[si + 2]);
        vecB.set(pos[ti], pos[ti + 1], pos[ti + 2]);
        const dx = vecB.x - vecA.x;
        const dy = vecB.y - vecA.y;
        const dz = vecB.z - vecA.z;
        const length = Math.hypot(dx, dy, dz) || 1e-4;
        const ux = dx / length;
        const uy = dy / length;
        const uz = dz / length;
        const gapS = Math.min(drawnRadius[link.s], length * 0.4);
        const gapT = Math.min(drawnRadius[link.t], length * 0.4);

        const focused = focus >= 0 && (link.s === focus || link.t === focus);
        let a: number;
        if (focused) {
          scratch.copy(EDGE_FOCUS).multiplyScalar(EDGE_GAIN * 1.4);
          a = 0.7 * thickness;
        } else if (focus >= 0) {
          scratch.copy(EDGE_BASE).multiplyScalar(EDGE_GAIN * 0.5);
          a = 0.09 * thickness;
        } else {
          mixColor.copy(finalColors[link.s]).lerp(finalColors[link.t], 0.5);
          scratch.copy(EDGE_BASE).lerp(mixColor, 0.35).multiplyScalar(EDGE_GAIN);
          a = 0.42 * thickness;
        }
        // an endpoint that drifts back out dims its lines instead of snapping them
        const rest = Math.min(field.integration[link.s], field.integration[link.t]);
        a *= gate * smooth(draw / EDGE_ALPHA_FRACTION)
          * (0.35 + 0.65 * clamp((rest - EDGE_DROP) / (EDGE_GATE - EDGE_DROP), 0, 1));

        const grown = smoother(draw);
        const ax = vecA.x + ux * gapS;
        const ay = vecA.y + uy * gapS;
        const az = vecA.z + uz * gapS;
        const bx = vecB.x - ux * gapT;
        const by = vecB.y - uy * gapT;
        const bz = vecB.z - uz * gapT;
        const headX = link.reversed ? bx : ax;
        const headY = link.reversed ? by : ay;
        const headZ = link.reversed ? bz : az;
        const tailX = link.reversed ? ax : bx;
        const tailY = link.reversed ? ay : by;
        const tailZ = link.reversed ? az : bz;

        edgePositions[vertex * 3] = headX;
        edgePositions[vertex * 3 + 1] = headY;
        edgePositions[vertex * 3 + 2] = headZ;
        edgeColors[vertex * 3] = scratch.r;
        edgeColors[vertex * 3 + 1] = scratch.g;
        edgeColors[vertex * 3 + 2] = scratch.b;
        edgeAlphas[vertex] = a;
        vertex += 1;
        edgePositions[vertex * 3] = headX + (tailX - headX) * grown;
        edgePositions[vertex * 3 + 1] = headY + (tailY - headY) * grown;
        edgePositions[vertex * 3 + 2] = headZ + (tailZ - headZ) * grown;
        edgeColors[vertex * 3] = scratch.r;
        edgeColors[vertex * 3 + 1] = scratch.g;
        edgeColors[vertex * 3 + 2] = scratch.b;
        edgeAlphas[vertex] = a;
        vertex += 1;
      }
      edgeGeometry.setDrawRange(0, vertex);
      edgeGeometry.attributes.position.needsUpdate = true;
      edgeGeometry.attributes.aColor.needsUpdate = true;
      edgeGeometry.attributes.aAlpha.needsUpdate = true;
    };

    /* ----------------------------------------------------------- hover label */
    const projected = new Vector3();
    const drawLabel = () => {
      if (!ctx) return;
      const dpr = renderer.getPixelRatio();
      const width = overlay.width / dpr;
      const height = overlay.height / dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      if (hovered < 0 || alpha[hovered] <= 0.2) return;
      const node = slotNode[hovered];
      if (!node) return;
      const at = hovered * 3;
      projected.set(pos[at], pos[at + 1], pos[at + 2]).project(camera);
      if (projected.z > 1) return;
      const x = (projected.x * 0.5 + 0.5) * width;
      const y = (-projected.y * 0.5 + 0.5) * height;
      const raw = node.label;
      const label = raw.length > 40 ? `${raw.slice(0, 39)}…` : raw;
      ctx.font = '500 12px "Pretendard Variable", Pretendard, system-ui, sans-serif';
      const boxW = ctx.measureText(label).width + 18;
      const boxH = 22;
      const boxX = clamp(x - boxW / 2, 6, Math.max(6, width - boxW - 6));
      const boxY = clamp(y + drawnRadius[hovered] + 10, 6, Math.max(6, height - boxH - 6));
      ctx.beginPath();
      ctx.roundRect(boxX, boxY, boxW, boxH, 6);
      ctx.fillStyle = "rgba(29,31,35,.94)";
      ctx.fill();
      ctx.strokeStyle = "rgba(53,55,61,.9)";
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = "rgba(228,229,232,.98)";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(label, boxX + 9, boxY + boxH / 2 + 0.5);
    };

    /* ---------------------------------------------------------- camera moves */
    let tween: {from: Vector3; to: Vector3; fromTarget: Vector3; toTarget: Vector3; start: number; duration: number} | null = null;
    const startTween = (to: Vector3, toTarget: Vector3, duration: number) => {
      tween = {
        from: camera.position.clone(),
        to,
        fromTarget: controls.target.clone(),
        toTarget,
        start: performance.now(),
        duration: reduced ? 0 : duration,
      };
    };
    const zoomBy = (factor: number) => {
      const offset = camera.position.clone().sub(controls.target);
      const distance = clamp(offset.length() * factor, controls.minDistance, controls.maxDistance);
      startTween(controls.target.clone().add(offset.setLength(distance)), controls.target.clone(), 260);
    };
    /** Fit to the live visible bounds, never to a theoretical radius. */
    const fit = () => {
      let minX = Infinity; let minY = Infinity; let minZ = Infinity;
      let maxX = -Infinity; let maxY = -Infinity; let maxZ = -Infinity;
      let found = 0;
      for (let slot = 0; slot < capacity; slot += 1) {
        if (alpha[slot] <= 0.2) continue;
        const at = slot * 3;
        const r = Math.max(drawnRadius[slot], baseRadius[slot]) * 1.2;
        minX = Math.min(minX, pos[at] - r); maxX = Math.max(maxX, pos[at] + r);
        minY = Math.min(minY, pos[at + 1] - r); maxY = Math.max(maxY, pos[at + 1] + r);
        minZ = Math.min(minZ, pos[at + 2] - r); maxZ = Math.max(maxZ, pos[at + 2] + r);
        found += 1;
      }
      if (!found) return;
      const center = new Vector3((minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2);
      const radius = Math.max(1, 0.5 * Math.hypot(maxX - minX, maxY - minY, maxZ - minZ));
      const vFov = (camera.fov * Math.PI) / 180;
      const hFov = 2 * Math.atan(Math.tan(vFov / 2) * camera.aspect);
      const distance = clamp(
        Math.max(radius / Math.sin(vFov / 2), radius / Math.sin(hFov / 2)) * 1.04,
        controls.minDistance + radius,
        controls.maxDistance,
      );
      const direction = camera.position.clone().sub(controls.target);
      if (direction.lengthSq() < 1e-6) direction.set(0, 0, 1);
      direction.normalize();
      startTween(center.clone().addScaledVector(direction, distance), center, 420);
    };

    /* -------------------------------------------------- screen-space picking */
    const pickIndex = (clientX: number, clientY: number) => {
      const rect = gl.getBoundingClientRect();
      if (!rect.width || !rect.height) return -1;
      const px = clientX - rect.left;
      const py = clientY - rect.top;
      const projectScale = (rect.height * 0.5) / Math.tan(((camera.fov * Math.PI) / 180) / 2);
      let best = -1;
      let bestScore = Infinity;
      for (let slot = 0; slot < capacity; slot += 1) {
        if (alpha[slot] <= 0.2) continue;
        const at = slot * 3;
        vecA.set(pos[at], pos[at + 1], pos[at + 2]);
        const depth = camera.position.distanceTo(vecA);
        projected.copy(vecA).project(camera);
        if (projected.z > 1 || projected.z < -1) continue;
        const sx = (projected.x * 0.5 + 0.5) * rect.width;
        const sy = (-projected.y * 0.5 + 0.5) * rect.height;
        const screenDistance = Math.hypot(px - sx, py - sy);
        const pixelRadius = (Math.max(drawnRadius[slot], baseRadius[slot] * 0.4) * projectScale) / Math.max(1e-3, depth);
        if (screenDistance > Math.max(pixelRadius + 5, 9)) continue;
        const score = screenDistance - pixelRadius + depth * 1e-4;
        if (score < bestScore) { bestScore = score; best = slot; }
      }
      return best;
    };
    const planePoint = (clientX: number, clientY: number, target: Vector3) => {
      const rect = gl.getBoundingClientRect();
      const ndcX = ((clientX - rect.left) / rect.width) * 2 - 1;
      const ndcY = -((clientY - rect.top) / rect.height) * 2 + 1;
      const origin = camera.position.clone();
      const direction = new Vector3(ndcX, ndcY, 0.5).unproject(camera).sub(origin).normalize();
      const denominator = dragPlane.normal.dot(direction);
      if (Math.abs(denominator) < 1e-6) return null;
      const t = -(dragPlane.normal.dot(origin) + dragPlane.constant) / denominator;
      if (t < 0) return null;
      return target.copy(origin).addScaledVector(direction, t);
    };

    /* ----------------------------------------------------------- interaction */
    let downX = 0;
    let downY = 0;
    let downAt = 0;
    let lastDragEvent = 0;

    const endDrag = (select: boolean) => {
      if (draggingSlot < 0) return;
      const slot = draggingSlot;
      draggingSlot = -1;
      field.pinned[slot] = 0;
      field.markHeld(slot, false);
      if (dragPointer >= 0 && gl.hasPointerCapture(dragPointer)) {
        try { gl.releasePointerCapture(dragPointer); } catch { /* already released */ }
      }
      dragPointer = -1;
      dragCaptured = false;
      controls.enabled = true;
      if (dragMoved) {
        // released into the live field: the neighbourhood absorbs it organically
        returningLoosen = slot;
      } else if (select && slotNode[slot]) {
        selectRef.current(slotNode[slot]!.id);
      }
      hovered = -1;
      dirty = true;
    };

    const onPointerDown = (event: PointerEvent) => {
      downX = event.clientX;
      downY = event.clientY;
      downAt = event.timeStamp;
      if (event.button !== 0) return;
      const slot = pickIndex(event.clientX, event.clientY);
      if (slot < 0) return;  // empty space -> OrbitControls owns the gesture
      draggingSlot = slot;
      dragMoved = false;
      dragPointer = event.pointerId;
      lastDragEvent = performance.now();
      field.pinned[slot] = 1;
      field.markHeld(slot, true);
      const at = slot * 3;
      dragOrigin.set(pos[at], pos[at + 1], pos[at + 2]);
      camera.getWorldDirection(vecB);
      dragPlane.setFromNormalAndCoplanarPoint(vecB, dragOrigin);
      controls.enabled = false;
      dragCaptured = false;
      try { gl.setPointerCapture(event.pointerId); dragCaptured = true; } catch { /* unsupported */ }
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    const onPointerMove = (event: PointerEvent) => {
      if (draggingSlot >= 0) {
        if (planePoint(event.clientX, event.clientY, vecA)) {
          const at = draggingSlot * 3;
          pos[at] = vecA.x; pos[at + 1] = vecA.y; pos[at + 2] = vecA.z;
          field.px[draggingSlot] = vecA.x / worldScale;
          field.py[draggingSlot] = vecA.y / worldScale;
          field.pz[draggingSlot] = vecA.z / worldScale;
          if (!dragMoved && Math.hypot(event.clientX - downX, event.clientY - downY) > CLICK_SLOP) dragMoved = true;
          dirty = true;
        }
        lastDragEvent = performance.now();
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      const next = pickIndex(event.clientX, event.clientY);
      if (next !== hovered) {
        hovered = next;
        gl.style.cursor = next >= 0 ? "pointer" : "default";
        dirty = true;
      }
    };
    const onPointerUp = (event: PointerEvent) => {
      if (event.defaultPrevented) return;
      if (draggingSlot >= 0) {
        endDrag(true);
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      if (event.button !== 0) return;
      if (Math.hypot(event.clientX - downX, event.clientY - downY) > CLICK_SLOP) return;
      if (event.timeStamp - downAt > 700) return;
      const slot = pickIndex(event.clientX, event.clientY);
      selectRef.current(slot >= 0 ? slotNode[slot]?.id ?? null : null);
    };
    const onPointerCancel = () => endDrag(false);
    const onLostCapture = () => endDrag(false);
    /* A release can land anywhere — outside the canvas, on another element, or
     * with the capture already gone. Window-level fallbacks plus a watchdog
     * guarantee a dragged node is never left pinned to a dead pointer. */
    const onWindowPointerUp = (event: PointerEvent) => {
      if (draggingSlot < 0) return;
      endDrag(true);
      event.preventDefault();
    };
    const onBlur = () => { endDrag(false); hovered = -1; dirty = true; };
    /* Coming back from a hidden tab resumes exactly where it left off. The
     * elapsed wall time is thrown away, never simulated. */
    const onVisibility = () => {
      if (document.visibilityState !== "visible") return;
      lastFrameAt = performance.now();
      accumulator = 0;
    };
    const onPointerLeave = () => { if (draggingSlot < 0) { hovered = -1; dirty = true; } };
    const onContextMenu = (event: MouseEvent) => event.preventDefault();
    const onMotionPreference = (event: MediaQueryListEvent) => {
      reduced = event.matches;
      if (reduced) settleImmediately();
      dirty = true;
    };

    gl.addEventListener("pointerdown", onPointerDown, true);
    gl.addEventListener("pointermove", onPointerMove, true);
    gl.addEventListener("pointerup", onPointerUp, true);
    gl.addEventListener("pointercancel", onPointerCancel, true);
    gl.addEventListener("lostpointercapture", onLostCapture);
    gl.addEventListener("pointerleave", onPointerLeave);
    gl.addEventListener("contextmenu", onContextMenu);
    window.addEventListener("pointerup", onWindowPointerUp, true);
    window.addEventListener("pointercancel", onPointerCancel, true);
    window.addEventListener("blur", onBlur);
    document.addEventListener("visibilitychange", onVisibility);
    motionQuery?.addEventListener("change", onMotionPreference);

    /* ----------------------------------------------------------------- frame */
    const resize = () => {
      const width = hostEl.clientWidth || 1;
      const height = hostEl.clientHeight || 1;
      renderer.setSize(width, height, false);
      composer.setSize(width, height);
      bloomPass.setSize(width, height);
      const dpr = renderer.getPixelRatio();
      overlay.width = Math.round(width * dpr);
      overlay.height = Math.round(height * dpr);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      dirty = true;
    };
    const observer = new ResizeObserver(resize);
    observer.observe(hostEl);
    resize();
    sync(dataRef.current);
    ballRadius = expectedRadius();
    camera.position.set(0, 0, ballRadius * CAMERA_PULLBACK);
    fog.density = 0.17 / Math.max(1, ballRadius);
    controls.maxDistance = ballRadius * 120;
    camera.far = Math.max(2000, ballRadius * 400);
    camera.updateProjectionMatrix();
    replay();
    /* 첫 화면은 이미 완성된 그래프다. 처음부터 한 노드씩 자라는 모습은 재생
     * 버튼이 맡고, 그 뒤로는 ingest가 실어 오는 새 노드만 arrivals 레인을 타고
     * 살아난다. replay()를 먼저 부르는 건 장부(간격·엣지 페이스·메모리)를
     * 초기 상태로 세우기 위해서고, 그 위에 완성 상태를 덮는다. */
    settleImmediately();

    /**
     * Read-only runtime probe for QA. It reports; it never changes anything the
     * viewer sees.
     *
     * In a development build it additionally honours `probe.timeScale`, so a
     * test can watch an eight-minute formation in half a minute. That hook does
     * not survive into a production build: `import.meta.env.DEV` is a literal at
     * build time, so the branch below is eliminated, the multiplier is the
     * constant 1, and the exposed probe is frozen — nothing on the page can
     * accelerate, slow, or otherwise steer the field.
     */
    const probeKey = "__llmwikiGraphProbe";
    const qaClock = import.meta.env.DEV;
    const timeScale = qaClock
      ? () => {
          const raw = (probe as unknown as Record<string, unknown>).timeScale;
          const value = typeof raw === "number" && Number.isFinite(raw) ? raw : 1;
          return clamp(value, 0.05, 60);
        }
      : () => 1;
    /**
     * How round the cloud actually is. A bounding box hides a filament whenever
     * it is not axis-aligned, so this reads the principal axes instead: the
     * ratio of the largest to the smallest standard deviation, where 1 is a
     * perfect ball. Closed-form eigenvalues of a symmetric 3x3.
     */
    const anisotropy = () => {
      let mx = 0; let my = 0; let mz = 0; let n = 0;
      for (let slot = 0; slot < capacity; slot += 1) {
        if (alpha[slot] <= 0.004) continue;
        mx += pos[slot * 3]; my += pos[slot * 3 + 1]; mz += pos[slot * 3 + 2];
        n += 1;
      }
      if (n < 4) return {sigma: [0, 0, 0], ratio: 1};
      mx /= n; my /= n; mz /= n;
      let xx = 0; let yy = 0; let zz = 0; let xy = 0; let xz = 0; let yz = 0;
      for (let slot = 0; slot < capacity; slot += 1) {
        if (alpha[slot] <= 0.004) continue;
        const a = pos[slot * 3] - mx;
        const b = pos[slot * 3 + 1] - my;
        const c = pos[slot * 3 + 2] - mz;
        xx += a * a; yy += b * b; zz += c * c; xy += a * b; xz += a * c; yz += b * c;
      }
      xx /= n; yy /= n; zz /= n; xy /= n; xz /= n; yz /= n;
      const q = (xx + yy + zz) / 3;
      const p2 = (xx - q) ** 2 + (yy - q) ** 2 + (zz - q) ** 2 + 2 * (xy * xy + xz * xz + yz * yz);
      const pp = Math.sqrt(p2 / 6);
      if (!(pp > 1e-12)) return {sigma: [0, 0, 0], ratio: 1};
      const b00 = (xx - q) / pp; const b11 = (yy - q) / pp; const b22 = (zz - q) / pp;
      const b01 = xy / pp; const b02 = xz / pp; const b12 = yz / pp;
      const det = b00 * (b11 * b22 - b12 * b12) - b01 * (b01 * b22 - b12 * b02) + b02 * (b01 * b12 - b11 * b02);
      const phi = Math.acos(clamp(det / 2, -1, 1)) / 3;
      const e1 = q + 2 * pp * Math.cos(phi);
      const e3 = q + 2 * pp * Math.cos(phi + (2 * Math.PI) / 3);
      const e2 = 3 * q - e1 - e3;
      const sigma = [e1, e2, e3].map((v) => Math.sqrt(Math.max(0, v))).sort((a, b) => b - a);
      return {
        sigma: sigma.map((v) => +v.toFixed(1)),
        ratio: +(sigma[0] / Math.max(1e-6, sigma[2])).toFixed(3),
      };
    };

    /** Mean speed split by whether a node currently has a link coming alive. */
    const responseSplit = () => {
      const ramping = new Uint8Array(capacity);
      let live = 0;
      for (let index = 0; index < links.length; index += 1) {
        const gain = field.linkGain[index];
        if (gain <= 0.02 || gain >= 0.98) continue;
        live += 1;
        ramping[links[index].s] = 1;
        ramping[links[index].t] = 1;
      }
      let hotSum = 0; let hotN = 0; let coldSum = 0; let coldN = 0;
      for (let slot = 0; slot < capacity; slot += 1) {
        if (!field.awake[slot] || field.integration[slot] < EDGE_GATE) continue;
        if (ramping[slot]) { hotSum += field.speed[slot]; hotN += 1; }
        else { coldSum += field.speed[slot]; coldN += 1; }
      }
      /* What connecting actually did to the geometry: endpoint separation now
       * against separation at the moment the line appeared. */
      let spanNow = 0; let spanThen = 0; let spanN = 0; let pendingSpan = 0; let pendingN = 0;
      const wired: number[] = [];
      for (let index = 0; index < links.length; index += 1) {
        const link = links[index];
        if (!field.awake[link.s] || !field.awake[link.t]) continue;
        const distance = Math.hypot(
          field.px[link.t] - field.px[link.s],
          field.py[link.t] - field.py[link.s],
          field.pz[link.t] - field.pz[link.s],
        );
        if (!Number.isFinite(link.readyAt)) { pendingSpan += distance; pendingN += 1; continue; }
        spanNow += distance; spanThen += link.spanAtReveal; spanN += 1;
        wired.push(distance);
      }

      /* Separation between nodes that are *not* joined by a link, for contrast.
       * Sampled on a fixed stride so the cost stays flat on big graphs. */
      const linkedPairs = new Set<number>();
      for (const link of links) {
        if (!Number.isFinite(link.readyAt)) continue;
        linkedPairs.add(link.s < link.t ? link.s * capacity + link.t : link.t * capacity + link.s);
      }
      const loose: number[] = [];
      for (let a = 0; a < capacity; a += 1) {
        if (!field.awake[a]) continue;
        for (let b = a + 1; b < capacity; b += 3) {
          if (!field.awake[b]) continue;
          if (linkedPairs.has(a * capacity + b)) continue;
          loose.push(Math.hypot(field.px[b] - field.px[a], field.py[b] - field.py[a], field.pz[b] - field.pz[a]));
        }
      }

      const pct = (sorted: number[], q: number) =>
        sorted.length ? +sorted[Math.floor((sorted.length - 1) * q)].toFixed(3) : 0;
      wired.sort((a, b) => a - b);
      loose.sort((a, b) => a - b);
      // fine histogram: a rest length would show as a spike, a floor as a wall
      const top = wired.length ? wired[wired.length - 1] : 1;
      const bins = new Array(24).fill(0) as number[];
      for (const d of wired) bins[Math.min(23, Math.floor((d / top) * 24))] += 1;

      return {
        rampingLinks: live,
        respondingNodes: hotN,
        meanSpeedResponding: +(hotN ? hotSum / hotN : 0).toFixed(5),
        meanSpeedQuiet: +(coldN ? coldSum / coldN : 0).toFixed(5),
        connectedSpanAtReveal: +(spanN ? spanThen / spanN : 0).toFixed(3),
        connectedSpanNow: +(spanN ? spanNow / spanN : 0).toFixed(3),
        unconnectedSpanNow: +(pendingN ? pendingSpan / pendingN : 0).toFixed(3),
        connectedDistance: {
          n: wired.length,
          min: pct(wired, 0), p10: pct(wired, 0.1), p25: pct(wired, 0.25), p50: pct(wired, 0.5),
          p75: pct(wired, 0.75), p90: pct(wired, 0.9), max: pct(wired, 1),
        },
        unconnectedDistance: {
          n: loose.length,
          min: pct(loose, 0), p10: pct(loose, 0.1), p50: pct(loose, 0.5), p90: pct(loose, 0.9),
        },
        connectedHistogram: {binWidth: +(top / 24).toFixed(3), counts: bins},
        minLinkForce: +field.minLinkForce.toFixed(4),
        closestActiveLink: +field.closestActiveLink.toFixed(3),
      };
    };

    const probe = () => {
      let minX = Infinity; let minY = Infinity; let minZ = Infinity;
      let maxX = -Infinity; let maxY = -Infinity; let maxZ = -Infinity;
      let liveN = 0;
      let lit = 0;
      let igniting = 0;
      let maxRadius = 0;
      let speedSum = 0;
      let igniteSum = 0;
      let coreX = 0; let coreY = 0; let coreZ = 0; let coreW = 0;
      for (let slot = 0; slot < capacity; slot += 1) {
        if (alpha[slot] <= 0.004) continue;
        const at = slot * 3;
        minX = Math.min(minX, pos[at]); maxX = Math.max(maxX, pos[at]);
        minY = Math.min(minY, pos[at + 1]); maxY = Math.max(maxY, pos[at + 1]);
        minZ = Math.min(minZ, pos[at + 2]); maxZ = Math.max(maxZ, pos[at + 2]);
        maxRadius = Math.max(maxRadius, Math.hypot(pos[at], pos[at + 1], pos[at + 2]));
        speedSum += field.speed[slot];
        igniteSum += field.ignite[slot];
        liveN += 1;
        if (field.ignite[slot] >= 0.999) lit += 1;
        else if (field.ignite[slot] > 0.02) igniting += 1;
        const w = field.settle[slot] * field.settle[slot];
        if (w > 0) { coreX += pos[at] * w; coreY += pos[at + 1] * w; coreZ += pos[at + 2] * w; coreW += w; }
      }
      const shells = new Array(10).fill(0) as number[];
      for (let slot = 0; slot < capacity; slot += 1) {
        if (alpha[slot] <= 0.004) continue;
        const at = slot * 3;
        const r = Math.hypot(pos[at], pos[at + 1], pos[at + 2]) / (maxRadius || 1);
        shells[Math.min(9, Math.floor(r * 10))] += 1;
      }
      const ex = maxX - minX;
      const ey = maxY - minY;
      const ez = maxZ - minZ;
      const mean = (ex + ey + ez) / 3 || 1;
      let visibleEdges = 0;
      for (const link of links) {
        if (spanProgress(clock, link.readyAt, link.drawMs) > 0.01
          && Math.min(alpha[link.s], alpha[link.t]) > 0.01) visibleEdges += 1;
      }
      return {
        reducedMotion: reduced,
        generation,
        replayCount,
        clockMs: Math.round(clock),
        timeScale: timeScale(),
        awakeNodes: field.activeCount,
        liveNodes: liveN,
        settledNodes: field.settledCount(EDGE_GATE),
        litNodes: lit,
        ignitingNodes: igniting,
        meanIgnite: +(liveN ? igniteSum / liveN : 0).toFixed(3),
        totalNodes: slotOf.size,
        pendingFormation: formation.length,
        pendingArrivals: arrivals.length,
        meanSpeed: +(liveN ? speedSum / liveN : 0).toFixed(4),
        maxSpeed: +field.maxSpeed.toFixed(4),
        coreSpeed: +field.coreSpeed.toFixed(4),
        coreDrift: +field.coreDrift.toFixed(4),
        coreCentroid: coreW > 0
          ? {x: +(coreX / coreW).toFixed(2), y: +(coreY / coreW).toFixed(2), z: +(coreZ / coreW).toFixed(2)}
          : {x: 0, y: 0, z: 0},
        visibleEdges,
        totalLinks: links.length,
        edgeIntervalMs: +edgeRevealInterval.toFixed(0),
        ...responseSplit(),
        cameraDistance: +camera.position.distanceTo(controls.target).toFixed(1),
        worldScale: +worldScale.toFixed(2),
        extents: {x: +ex.toFixed(1), y: +ey.toFixed(1), z: +ez.toFixed(1)},
        extentRatios: {x: +(ex / mean).toFixed(3), y: +(ey / mean).toFixed(3), z: +(ez / mean).toFixed(3)},
        principalAxes: anisotropy(),
        radialShells: shells,
        ballRadius: +ballRadius.toFixed(1),
        positionChecksum: Array.from(pos.slice(0, Math.min(pos.length, 12))).map((v) => +v.toFixed(2)),
      };
    };
    (window as unknown as Record<string, unknown>)[probeKey] = qaClock ? probe : Object.freeze(probe);

    let frame = 0;
    const loop = () => {
      if (disposed) return;
      frame = requestAnimationFrame(loop);
      const now = performance.now();
      const realDelta = Math.min(FRAME_CLAMP_MS, Math.max(0, now - lastFrameAt));
      lastFrameAt = now;
      const delta = realDelta * timeScale();
      clock += delta;
      /* Holding a node perfectly still produces no pointer traffic at all, so
       * silence must never be read as a release. When the browser gave us the
       * capture it will tell us if the pointer really goes away
       * (`lostpointercapture`), and the grab is held until it does or until the
       * viewer lets go. The timeout is only the fallback for the case where the
       * capture was refused and we have nothing else to go on. */
      if (draggingSlot >= 0 && !dragCaptured && now - lastDragEvent > 1500) endDrag(false);

      scanVisible();
      if (pumpQueues()) dirty = true;
      // the field is alive every frame unless the viewer asked for stillness
      if (advance(delta) || draggingSlot >= 0 || !reduced) dirty = true;

      if (tween) {
        const progress = smoother(spanProgress(now, tween.start, tween.duration));
        camera.position.lerpVectors(tween.from, tween.to, progress);
        controls.target.lerpVectors(tween.fromTarget, tween.toTarget, progress);
        if (progress >= 1) tween = null;
        needsRender = true;
      }
      if (dirty) { paint(); dirty = false; needsRender = true; }
      if (controls.update()) needsRender = true;
      if (needsRender) {
        composer.render();
        drawLabel();
        needsRender = false;
      }
    };
    frame = requestAnimationFrame(loop);

    const dispose = () => {
      if (disposed) return;
      disposed = true;
      cancelAnimationFrame(frame);
      observer.disconnect();
      const globals = window as unknown as Record<string, unknown>;
      if (globals[probeKey] === probe) delete globals[probeKey];
      gl.removeEventListener("pointerdown", onPointerDown, true);
      gl.removeEventListener("pointermove", onPointerMove, true);
      gl.removeEventListener("pointerup", onPointerUp, true);
      gl.removeEventListener("pointercancel", onPointerCancel, true);
      gl.removeEventListener("lostpointercapture", onLostCapture);
      gl.removeEventListener("pointerleave", onPointerLeave);
      gl.removeEventListener("contextmenu", onContextMenu);
      window.removeEventListener("pointerup", onWindowPointerUp, true);
      window.removeEventListener("pointercancel", onPointerCancel, true);
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("visibilitychange", onVisibility);
      motionQuery?.removeEventListener("change", onMotionPreference);
      controls.dispose();
      composer.dispose();
      coreMesh?.dispose();
      sphere.dispose();
      coreMaterial.dispose();
      haloGeometry?.dispose();
      haloMaterial.dispose();
      edgeGeometry.dispose();
      edgeMaterial.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      gl.remove();
      overlay.remove();
    };

    engineRef.current = {
      sync,
      setBackdrop: (color) => { backdrop.set(color); fog.color.set(color); dirty = true; },
      zoomBy,
      fit,
      replay,
      markDirty: () => { syncRadii(); colorKey = ""; dirty = true; },
      dispose,
    };

    return () => {
      engineRef.current = null;
      dispose();
    };
    // The engine owns its own lifetime: data arrives through `sync`, never a remount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* An ingest or an HMR reload is a merge, not a rebuild. */
  const firstData = useRef(true);
  useEffect(() => {
    if (firstData.current) { firstData.current = false; return; }
    engineRef.current?.sync(data);
  }, [data]);

  useEffect(() => { engineRef.current?.markDirty(); }, [visibleIds, selectedId, colorBy, showColors, settings]);

  /* 무대 배경은 테마를 따라간다. 값은 스타일시트가 쥐고 있어 CSS 한 곳에서만 바뀐다. */
  useEffect(() => {
    const hostEl = host.current;
    if (!hostEl) return;
    const backdrop = getComputedStyle(hostEl).getPropertyValue("--stage-backdrop").trim();
    if (backdrop) engineRef.current?.setBackdrop(backdrop);
  }, [theme]);
  useImperativeHandle(ref, () => ({
    zoomIn: () => engineRef.current?.zoomBy(0.6),
    zoomOut: () => engineRef.current?.zoomBy(1 / 0.6),
    reset: () => engineRef.current?.fit(),
    replay: () => engineRef.current?.replay(),
  }), []);

  return <div ref={host} className="graph-host graph-host-3d" role="application" aria-label="지식 그래프 3D 캔버스" />;
});

export default Graph3DCanvas;
