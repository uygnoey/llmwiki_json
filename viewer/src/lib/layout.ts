/**
 * Live soft-force field for the knowledge graph.
 *
 * There is no precomputed destination anywhere in this file. A node is lit at a
 * random position outside the cluster and is then drawn inward by the field
 * alone: short-range repulsion and collision relief keep neighbours apart,
 * real links pull them together, and a soft spherical skin keeps the aggregate
 * a ball. Where a node ends up is never written down — it is only ever the
 * result of the forces acting on it.
 *
 * Three properties are load-bearing and every constant below is chosen to
 * preserve them:
 *
 *  1. **Overdamped.** Integration is a fixed timestep with hard caps on
 *     acceleration, speed and per-step displacement. Pushed hard the field
 *     drifts back; it never rings, never bounces, never accelerates late.
 *  2. **Newcomer energy stays local.** Waking a node does not reheat anybody.
 *     Established nodes are throttled by a mobility weight, so an arrival is
 *     absorbed as a slow local adjustment instead of a global shudder. The
 *     recentering term is driven by the *settled core* and is rate-limited, so
 *     a node appearing far outside can never counter-translate the cluster.
 *  3. **No hidden schedule.** `ignite` is the only per-node clock, it starts
 *     the moment a node is created, and it exists purely so the renderer can
 *     begin lighting a node while it is still outside and travelling.
 *  4. **A link is a force exactly when it is a line.** `linkGain` is the same
 *     slow reveal progress the renderer draws with. A link that has not been
 *     revealed yet pulls on nothing; as its line grows in, its pull rises from
 *     zero and its two endpoints genuinely respond to each other. Nothing is
 *     ever wired between coordinates that were already decided.
 *  5. **No link has a target distance.** There is no rest length here, per-edge
 *     or global: a revealed link is pure attraction that never reaches zero and
 *     never flips to a push, however close its endpoints get. How far apart two
 *     connected nodes end up is therefore never written down anywhere — it is
 *     whatever the vector sum of that node's other links, the gentle repulsion
 *     of everything around it, real core-overlap relief and the spherical
 *     containment happen to balance at, and it keeps drifting as the graph
 *     around it changes.
 *
 * Simulation space is normalised: the mean node spacing is 1. The canvas
 * multiplies by a world scale derived from the "link distance" control, so
 * that control is a pure zoom of the same living field and never disturbs it.
 */

export interface LayoutLink {
  s: number;
  t: number;
}

/** Slider-driven force weights. */
export interface FieldTuning {
  repel: number;
  linkStrength: number;
  center: number;
}

/* ------------------------------------------------------------------ shaping
 * All lengths are multiples of the mean node spacing, which is 1 by
 * definition. Every force below is bounded from above: there is no term that
 * can grow without limit as the graph gets bigger or older. */
const REPEL_CUTOFF = 2.6;        // beyond this two nodes ignore each other
const REPEL_SOFT = 0.30;         // softening: the short-range push is gentle and
                                 // saturating, so nothing piles up on a wall
const REPEL_BASE = 0.062;        // per unit of the repel slider
/* Two nodes that the links have already pulled almost together must be able to
 * close the last gap. A 1/d^2 push is strongest exactly there, so on its own it
 * guarantees a floor no attraction can ever cross — which is what made every
 * connection stop at the same distance. `BOND_AT` fades the crowd pressure out
 * below that scale, so from there inward the only thing left is the overlap
 * guard: `COLLIDE_AT` is the radius at which two drawn cores would actually
 * intersect, a few hundredths of a spacing unit, and nothing wider than that is
 * defended. Pairs the topology pulls hard therefore end up touching, pairs it
 * pulls weakly never get inside the fade at all, and there is still no distance
 * anywhere that anything is aiming for. */
const BOND_AT = 0.95;            // below this the general repulsion eases away
const BOND_EXP = 2.2;
/* This is where two nodes actually hit each other: the sum of the radii they
 * are drawn at, and nothing more. It is not personal space and not a distance
 * they hold — a big soft bubble is what makes a connection look like it happens
 * "near" another node instead of on contact. It scales with degree only because
 * the drawn bodies do, and it is stiff, so meeting is an impact rather than a
 * slow settle into a cushion. */
/* Contact is not a constant. It is the sum of the two radii the renderer is
 * actually drawing, published into `bodyRadius` every frame with the very same
 * formula, times a hair of margin. Deriving it instead — a base times a degree
 * term, hand-fitted to whatever the renderer happened to do at the time — goes
 * stale the moment the drawn size changes for any reason, and then a pair can
 * pass every physical test for contact while the viewer plainly sees space
 * between the two dots. One source of truth for how big a node is. */
const CONTACT_MARGIN = 1.12;
const CONTACT_MIN = 0.012;       // never zero, whatever the size controls say
/* Sized so contact can actually answer the strongest pull in the engine. A
 * reaching link delivers force*gain — force ceilinged at MAX_LINK_FORCE*4, gain
 * reaching ~2.6 under REACH_ESCALATE — so roughly 21. With the old ceiling of 8
 * the collision term simply lost, and a pair slammed through its own contact
 * distance on the way in. COLLIDE_K in turn has to be able to reach that
 * ceiling before total overlap: at the smallest floor (COLLIDE_AT) the barrier
 * tops out at COLLIDE_K * COLLIDE_AT, so it is MAX_PAIR_FORCE / COLLIDE_AT.
 * Both are literals on purpose — writing them as expressions over
 * MAX_LINK_FORCE and COLLIDE_AT is a temporal-dead-zone error, those are
 * declared further down. The derivation lives here, in the comment. */
const COLLIDE_K = 640;           // stiff enough to reach MAX_PAIR_FORCE within a body
const MAX_PAIR_FORCE = 24;       // = MAX_LINK_FORCE * 4 * 3, the strongest a link can pull

/* ------------------------------------------------------------------ link pull
 * There is no rest length. `LINK_BASE` is the pull a fully revealed link
 * saturates at, and `LINK_SOFT` is the width over which that pull eases off as
 * the two nodes close in. The force is therefore strictly positive at every
 * distance: it never crosses zero, so it can never define an equilibrium radius
 * of its own, and it never turns into a push, so a pair is never bounced back
 * out. Far pairs feel almost the full pull, which behaves like a slack rope
 * rather than a spring and spreads the resulting distances out instead of
 * gathering them onto one length.
 *
 * `LINK_SOFT` is deliberately tiny. It exists only to keep the force finite as
 * the separation goes to zero; above it the pull is effectively flat. A pull
 * that faded as a pair closed in would be at its weakest exactly where it is
 * needed most, and any competing link could then peel a freshly joined pair
 * apart again — connections that visibly drift away from each other while
 * their line is still being drawn. Flat tension means what has come together
 * stays together, and what separates them is the crowd, not their own bond. */
const LINK_BASE = 1.45;          // per unit of the link-strength slider
const LINK_SOFT = 0.06;          // only enough softening to stay finite at zero
/* No two connections pull equally hard. The strength of a pair is read off the
 * topology alone — how much of their neighbourhood they share — so a pair that
 * sits in the same dense pocket of the graph reaches for each other far more
 * insistently than a pair that merely happens to be linked. Nothing here is a
 * distance: it only says who pulls harder, which is what makes the nodes drift
 * around and find each other at different moments instead of every neighbour
 * of a node arriving at once. */
const AFFINITY_MIN = 0.5;
const AFFINITY_MAX = 1.75;
/* A well connected node is a heavy one. It reaches further — every link that
 * touches it pulls harder by `DEGREE_PULL` — and it answers less, because
 * `HUB_MASS` makes it that much harder to move. So the crowd falls onto its
 * hubs rather than the hubs being dragged around by the crowd.
 *
 * `GROUP_PULL` is the same idea one level up, and it matters more: what hauls a
 * lone node in is not its one partner but everything that partner is already
 * joined to. A link into a large joined group pulls far harder than a link
 * between two strays, so groups gather nodes instead of nodes finding each
 * other one pair at a time. */
const DEGREE_PULL = 1.3;
/* Mass resists being *dragged*, but a joined group is supposed to be lively —
 * carrying its own momentum around the ball, not anchored by it. Keep it light
 * enough that tension can spin a group instead of stalling it. */
const HUB_MASS = 0.7;
const GROUP_PULL = 2.2;
const LINK_DEG_SOFT = 0.18;      // hubs feel the *average* of their *active* links
const MAX_LINK_FORCE = 2.0;
const DRAG_GAIN_MAX = 5;         // headroom for a link the viewer is pulling on
const MAX_LINK_TOTAL = 3.0;      // a hub's whole link budget, after softening

/* ------------------------------------------------------------- keeping a ball
 * Two isotropic terms, and nothing that knows about a coordinate. The skin only
 * bites outside the surface the members are currently occupying, so it pushes
 * a bulge back in without pressing on the cloud as a whole. The centre term
 * acts on everyone in proportion to how far out they are; it is what keeps the
 * aggregate round against a link network that is not itself round. Without it
 * the graph's own topology stretches the cloud into a spheroid — measured at
 * 1.57:1 on the principal axes, which a bounding box does not even reveal. */
const BOUNDARY_K = 70.0;         // a firm mould: the current is strong now, and
const BOUNDARY_MAX = 28.0;       // the surface has to be held against it
const BOUNDARY_SOFT = 0.02;      // fraction of R of slack before the skin engages
const CENTER_BASE = 9.0;         // per unit of the center slider
const CENTER_MAX = 8.0;          // the trap must keep rising all the way to the rim          // the trap must keep rising all the way to the
                                 // rim; capping it early leaves the outer shell
                                 // free to be pulled out of round by the links
const BALL_FILL = 1.0;           // R = cbrt(3N/4pi): the starting radius, before
                                 // the containment starts tracking the real cloud
const RADIUS_TAU = 5.0;          // s — the skin follows the cloud smoothly, so
                                 // neither waking a node nor a slow contraction
                                 // can ever step the boundary

/* How big the ball ends up is not decided here at all — it is wherever the
 * repulsion, the links and the skin balance. Making the *simulated* ball wider
 * than that equilibrium does not work: the links simply pull everyone back to
 * the middle and the skin, sitting far outside, never touches anything. If the
 * ball should look bigger, that is the world scale and the camera, not this. */
const MIN_FILL = 0.35;           // the skin can shrink with the cloud, but only
                                 // this far relative to uniform unit spacing

/* Nothing binds a node that has not connected to anything, so it does not sink
 * into the crowd — it feels only a fraction of the pull toward the middle and
 * ends up wandering the outer shell until something catches it. */
const RIM_FREEDOM = 0.25;
const WIRED_FULL = 1.6;          // live link weight at which a node is fully bound

const ACC_CAP = 5.0;             // units / s^2, at full mobility
const SPEED_CAP = 0.20;          // units / s, at full mobility (a newcomer's crawl)
const STEP_CAP = 0.008;          // units — absolute per-step displacement clamp
/* Damping decides whether a bond has any inertia. Held too tight, a pair that
 * has just linked up simply stops, which reads as two things clicking together
 * and dying. Loosened, the tension in a group carries it: it swings, it coasts,
 * it lags behind the current and swings back. The caps below still bound every
 * step, so the motion stays slow — it is momentum, not energy. */
const DAMP_TAU = 0.55;           // s — velocity half-life while joining
const DAMP_TAU_SETTLED = 0.60;   // s — a member of the cluster keeps its momentum

/* ------------------------------------------------------------------ mobility
 * Mobility is a function of *membership*, never of current speed. That matters:
 * a speed-driven weight is a positive feedback — a node that starts moving is
 * allowed to move faster, which is exactly how a gentle tug turns into a lurch.
 * Reading membership instead means an established node answers a new link at a
 * fixed, small crawl no matter how hard the link pulls. Never zero: it always
 * keeps enough budget to drift with its neighbourhood. */
const MOB_MIN = 0.33;
const MOB_EXP = 1.6;

/* ------------------------------------------------------- stability governors */
const RECENTER_TAU = 8.0;        // s — the cloud is nudged onto the origin
const RECENTER_MAX = 0.02;       // units / s — and never faster than this
const CORE_REST = 0.55;          // integration at which a node counts as core
/* A ceiling, not a regulator. The cluster is meant to be moving now, so this
 * sits well above the speed the current actually asks for and only catches a
 * runaway. Set it near the working speed and it brakes the whole ball every
 * step, which reads as the motion being held back. */
const CORE_SPEED_BUDGET = 0.09;  // units / s — mean core speed ceiling

const SPAWN_MIN = 1.42;          // spawn shell, in multiples of the current radius
const SPAWN_SPAN = 0.72;
const SPAWN_BIAS = 0.25;         // how far the random direction leans toward kin
const SPAWN_INWARD = 0.035;      // units / s of initial drift toward the middle

/* ----------------------------------------------------------------- settling */
const SPEED_EMA_TAU = 0.9;       // s
const SETTLE_SPEED_HI = 0.085;   // above this a node counts as clearly moving
const SETTLE_SPEED_LO = 0.012;   // below this it is effectively at rest
const SETTLE_RISE_TAU = 3.2;     // s — a node earns its place slowly
const SETTLE_FALL_TAU = 0.5;     // s — but a disturbance clears it faster
const MIN_SETTLE_AGE = 6.0;      // s a node must live before it may settle at all
const INSIDE_SLACK = 1.12;       // r/R below which a node counts as inside
const INSIDE_SPAN = 0.55;        // width, in R, of the "arriving" band

/* --------------------------------------------------------------- integration
 * How much a node counts as part of the cluster. Unlike `settle` this ignores
 * speed, so a node that is busy reacting to a link that has just come alive
 * does not stop being a member of the graph — otherwise wiring would gate
 * itself off the moment it started to do anything. */
const INTEGRATE_RISE_TAU = 4.0;  // s
const INTEGRATE_FALL_TAU = 1.2;  // s

/* ---------------------------------------------------------------------- flow
 * The cluster is never still. A slow, smooth current runs through it and
 * carries whatever is standing in it along. The wavelength is deliberately
 * several times the whole ball, so two nodes that have come together feel
 * almost exactly the same current and travel as one — which is the difference
 * between a group drifting and a group being shaken apart. It moves positions,
 * not velocities, so it adds no energy for the damping to fight, it is bounded
 * by construction, and the per-step displacement clamp still governs it. */
/* The wavelength is the whole point. Make it much larger than the ball and the
 * current becomes a uniform translation: every group moves identically, which
 * is the same as nothing moving, and the recentering quietly cancels what is
 * left. It has to be *shorter* than the cluster and *longer* than a joined
 * pair — then opposite sides of the ball are carried opposite ways, groups
 * wander past each other and actually run into things, while two nodes that
 * have linked up still sit in nearly the same current and travel together. */
/* Relative to the ball, not absolute. The cluster roughly doubles in size as it
 * fills, so a fixed speed means a node that used to cross half its own spacing
 * in five seconds ends up crossing a fifth of it — measurably still moving, and
 * visibly almost stopped. Scaling with the radius keeps the motion the viewer
 * actually perceives constant from the first node to the last. */
const FLOW_SPEED = 0.013;        // units / s *per unit of the implied radius*
/* The wavelength is a fraction of the ball, never an absolute length. Fix it in
 * absolute units and the moment the cluster contracts past that size the whole
 * cloud sits inside a single lobe again — one uniform drift, no relative motion
 * at all, and the recentering quietly removes even that. Tying it to the
 * current radius keeps the same number of counter-flowing regions across the
 * cluster whatever size the cluster happens to be. */
const FLOW_WAVES = 1.55;         // wavelengths across the ball's radius
const FLOW_HZ = 0.030;           // ~33 s: the current keeps changing direction, so
                                 // no single axis is favoured long enough to stretch it
/* On top of the current, the whole cluster turns. A rigid rotation is the one
 * motion that is guaranteed to disturb nothing at all — every relative distance
 * inside the ball is untouched — so it can be as visible as it likes. The axis
 * precesses, so it never reads as a turntable. */
const SWIRL_RATE = 0.034;        // rad / s — a full turn in about three minutes
const SWIRL_DRIFT = 0.021;       // how fast the axis itself wanders
const SWIRL_SHEAR = 0.55;        // shells slide past each other: a rigid turn of a
                                 // round cloud is, by itself, invisible
                                 // so groups sweep past each other instead of
                                 // co-rotating forever and never meeting

/* ------------------------------------------------------------------ ignition
 * Light begins at creation, not at settlement: `ignite` starts climbing the
 * instant a node exists and is completely independent of where it is. */
const IGNITE_SECONDS = 26;

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
const length3 = (x: number, y: number, z: number) => Math.sqrt(x * x + y * y + z * z);

/** Small, fast, fully deterministic PRNG. */
export function mulberry32(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Content-derived seed: the same graph always gets the same universe. */
export function layoutSeed(ids: readonly string[]): number {
  let hash = 0x811c9dc5;
  for (const id of ids) {
    for (let index = 0; index < id.length; index += 1) {
      hash ^= id.charCodeAt(index);
      hash = Math.imul(hash, 0x01000193);
    }
    hash = Math.imul(hash ^ 0x5f, 0x01000193);
  }
  return hash >>> 0;
}

/** Radius, in spacing units, that `count` nodes want to occupy. */
export const radiusForCount = (count: number) =>
  Math.max(1, BALL_FILL * Math.cbrt((3 * Math.max(1, count)) / (4 * Math.PI)));

/**
 * Reveal order: hub-first breadth-first, largest component first. The field
 * does not need it — it only decides *which* node is woken next, so the graph
 * grows outward from its densest points instead of from random dust.
 */
export function revealOrder(count: number, links: readonly LayoutLink[]): Int32Array {
  const degree = new Int32Array(count);
  for (const link of links) {
    degree[link.s] += 1;
    degree[link.t] += 1;
  }
  const start = new Int32Array(count + 1);
  for (let index = 0; index < count; index += 1) start[index + 1] = start[index] + degree[index];
  const cursor = start.slice(0, count);
  const neighbor = new Int32Array(links.length * 2);
  for (const link of links) {
    neighbor[cursor[link.s]++] = link.t;
    neighbor[cursor[link.t]++] = link.s;
  }

  const byDegree = Array.from({length: count}, (_, index) => index)
    .sort((a, b) => degree[b] - degree[a] || a - b);
  const order = new Int32Array(count);
  const seen = new Uint8Array(count);
  const queue = new Int32Array(count);
  let written = 0;
  for (const root of byDegree) {
    if (seen[root]) continue;
    seen[root] = 1;
    let head = 0;
    let tail = 0;
    queue[tail++] = root;
    while (head < tail) {
      const node = queue[head++];
      order[written++] = node;
      const fringe: number[] = [];
      for (let slot = start[node]; slot < start[node + 1]; slot += 1) {
        const next = neighbor[slot];
        if (seen[next]) continue;
        seen[next] = 1;
        fringe.push(next);
      }
      fringe.sort((a, b) => degree[b] - degree[a] || a - b);
      for (const next of fringe) queue[tail++] = next;
    }
  }
  return order;
}

/**
 * The field itself. Slots are stable: a node keeps its index for as long as it
 * exists, so an ingest can add newcomers without touching anybody else.
 */
export class MotionField {
  capacity = 0;
  /** Slots currently owned by a node. Slots beyond this are free. */
  count = 0;
  /** Nodes that have been woken and take part in the simulation. */
  activeCount = 0;

  px = new Float32Array(0);
  py = new Float32Array(0);
  pz = new Float32Array(0);
  vx = new Float32Array(0);
  vy = new Float32Array(0);
  vz = new Float32Array(0);
  /** 1 once the node has been woken. Asleep nodes are invisible and inert. */
  awake = new Uint8Array(0);
  /** 1 while a pointer owns the node — physics reads it but never writes it. */
  pinned = new Uint8Array(0);
  /** 1 while this node is a neighbour of something the viewer is holding. */
  heldNeighbour = new Uint8Array(0);
  /**
   * The node this one is hauling itself toward, or -1. Only that exact pair is
   * allowed to press closer than its personal space. Granting the squeeze to
   * any two nodes that merely happen to both be reaching — at anything, in any
   * direction — squeezes the entire crowd, and the cluster collapses into one
   * indistinguishable blob.
   */
  reachPartner = new Int32Array(0);
  /** 0 = just arrived or freshly disturbed, 1 = at rest inside a stable ball. */
  settle = new Float32Array(0);
  /** 0 = still outside or brand new, 1 = fully part of the cluster. Speed-blind. */
  integration = new Float32Array(0);
  /** 0 at creation, 1 once fully lit. Climbs on its own clock, wherever the node is. */
  ignite = new Float32Array(0);
  /** Smoothed speed, units per second. */
  speed = new Float32Array(0);
  /** Seconds since this node was woken. */
  age = new Float32Array(0);
  /** Deterministic per-node drift phases, so the float never repeats between nodes. */
  phase = new Float32Array(0);

  private fx = new Float32Array(0);
  private fy = new Float32Array(0);
  private fz = new Float32Array(0);
  private lx = new Float32Array(0);
  private ly = new Float32Array(0);
  private lz = new Float32Array(0);
  private rx = new Float32Array(0);
  private ry = new Float32Array(0);
  private rz = new Float32Array(0);
  private next = new Int32Array(0);

  /** CSR adjacency over the *current* topology. */
  private adjStart = new Int32Array(1);
  private adjList = new Int32Array(0);
  degree = new Int32Array(0);

  private linkS = new Int32Array(0);
  private linkT = new Int32Array(0);
  private linkCount = 0;
  /**
   * Per-link spring weight in [0, 1], written by the renderer every frame from
   * the very same reveal progress it draws the line with. 0 means the link is
   * not there yet and exerts nothing at all.
   */
  linkGain = new Float32Array(0);
  /**
   * The drawn part of that gain — reveal progress only, with the latent reach
   * excluded. A node is "caught" when lines have actually been drawn to it,
   * which is not the same as having neighbours reaching for it.
   */
  linkShown = new Float32Array(0);
  /**
   * Closest these two endpoints have ever been, sampled every substep rather
   * than once a frame. Sampling per frame makes "did they touch" depend on the
   * frame rate: an accelerated QA clock runs many substeps between samples and
   * overestimates the true minimum by up to a third of a contact distance, so
   * real collisions get missed and the accelerated run stops representing what
   * production actually does. Measured here, the gate is exact at any timestep.
   */
  linkMinSpan = new Float32Array(0);
  /**
   * 1 for the one unmade connection a node is currently reaching for. Marked
   * links bypass the hub softening and the per-node link budget entirely: the
   * whole point is that this pull is *not* averaged in with the nineteen others
   * already holding the node, because averaged in it is a fifteenth of the
   * force balance and nothing ever comes together.
   */
  linkReach = new Uint8Array(0);
  /** Per-link pull strength, from shared neighbourhood and degree. Topology only. */
  linkAffinity = new Float32Array(0);
  /** Degree on a 0..1 scale, p95-capped so a couple of mega-hubs cannot skew it. */
  degreeNorm = new Float32Array(0);
  /** Size of the joined group this node belongs to, 0..1. Written by the renderer. */
  groupMass = new Float32Array(0);
  /** The radius this node is actually drawn at, in simulation units. */
  bodyRadius = new Float32Array(0);
  /** Sum of active gains per node — hubs are softened by what is live, not by degree. */
  private linkLoad = new Float32Array(0);
  /** Sum of *drawn* link weight per node. */
  private shownLoad = new Float32Array(0);

  private head = new Int32Array(0);
  private hashMask = 0;
  private random: () => number;

  /** Seconds of field time, used only by the slow current. */
  private flowClock = 0;
  /** Seconds since the most recent node was lit — drives the contraction. */
  private sinceWake = 0;
  /** 1 while the ball is still wide open, 0 once it has finished closing. */
  containment = 1;
  /**
   * How hard the cluster is being stirred, 0..1, set by the renderer. It stays
   * at full while connections are still being made and eases down only once
   * nothing new has joined for a long while — so the field goes quiet because
   * the graph has finished, not because a timer said so.
   */
  stir = 1;
  /** Smoothed containment radius — never steps when a node wakes. */
  private radiusEma = 0;
  /** Relaxation ignores the slow-motion budget; it is only used off-screen. */
  private relaxing = false;

  /** Diagnostics for the read-only QA probe. Never read by the simulation. */
  maxSpeed = 0;
  coreSpeed = 0;
  coreDrift = 0;
  /**
   * The smallest per-link force produced last step, before gain and caps. The
   * link term is attraction by construction, so this is the number that proves
   * it: if it were ever negative some link would have pushed its endpoints
   * apart, which is exactly the rest-length behaviour this engine does not have.
   */
  minLinkForce = 0;
  /** Closest pair joined by a link that is currently pulling. */
  closestActiveLink = 0;

  constructor(seed: number) {
    this.random = mulberry32(seed ^ 0x2545f491);
  }

  /** Grow the slot arrays, preserving every value already in them. */
  ensureCapacity(wanted: number) {
    if (wanted <= this.capacity) return;
    const size = Math.max(64, 1 << Math.ceil(Math.log2(wanted + 1)));
    const grow = (source: Float32Array) => {
      const next = new Float32Array(size);
      next.set(source);
      return next;
    };
    const growI = (source: Int32Array) => {
      const next = new Int32Array(size);
      next.set(source);
      return next;
    };
    const growU = (source: Uint8Array) => {
      const next = new Uint8Array(size);
      next.set(source);
      return next;
    };
    this.px = grow(this.px); this.py = grow(this.py); this.pz = grow(this.pz);
    this.vx = grow(this.vx); this.vy = grow(this.vy); this.vz = grow(this.vz);
    this.fx = grow(this.fx); this.fy = grow(this.fy); this.fz = grow(this.fz);
    this.lx = grow(this.lx); this.ly = grow(this.ly); this.lz = grow(this.lz);
    this.rx = grow(this.rx); this.ry = grow(this.ry); this.rz = grow(this.rz);
    this.settle = grow(this.settle);
    this.integration = grow(this.integration);
    this.ignite = grow(this.ignite);
    this.speed = grow(this.speed);
    this.age = grow(this.age);
    this.phase = grow(this.phase);
    this.awake = growU(this.awake);
    this.pinned = growU(this.pinned);
    this.heldNeighbour = growU(this.heldNeighbour);
    this.reachPartner = growI(this.reachPartner);
    this.degree = growI(this.degree);
    this.degreeNorm = grow(this.degreeNorm);
    this.groupMass = grow(this.groupMass);
    this.bodyRadius = grow(this.bodyRadius);
    this.next = growI(this.next);
    this.linkLoad = grow(this.linkLoad);
    this.shownLoad = grow(this.shownLoad);
    for (let index = this.capacity; index < size; index += 1) {
      this.phase[index] = this.random() * Math.PI * 2;
    }
    this.capacity = size;
    const table = Math.max(256, 1 << Math.ceil(Math.log2(size * 2)));
    this.head = new Int32Array(table);
    this.hashMask = table - 1;
  }

  /** Replace the topology. Slot identity is untouched — only who links to whom. */
  setTopology(count: number, links: readonly LayoutLink[]) {
    this.ensureCapacity(count);
    this.count = count;
    this.linkCount = links.length;
    if (this.linkS.length < links.length) {
      this.linkS = new Int32Array(Math.max(64, links.length));
      this.linkT = new Int32Array(Math.max(64, links.length));
      this.linkGain = new Float32Array(Math.max(64, links.length));
      this.linkShown = new Float32Array(Math.max(64, links.length));
      this.linkMinSpan = new Float32Array(Math.max(64, links.length)).fill(Infinity);
      this.linkReach = new Uint8Array(Math.max(64, links.length));
      this.linkAffinity = new Float32Array(Math.max(64, links.length));
    }
    // link indices are rebuilt from the payload; the renderer re-supplies every
    // gain from its own edge memory before the next step, so start from silent
    this.linkGain.fill(0);
    this.linkShown.fill(0);
    this.linkMinSpan.fill(Infinity);
    this.linkReach.fill(0);
    this.degree.fill(0, 0, this.capacity);
    for (let index = 0; index < links.length; index += 1) {
      this.linkS[index] = links[index].s;
      this.linkT[index] = links[index].t;
      this.degree[links[index].s] += 1;
      this.degree[links[index].t] += 1;
    }
    const start = new Int32Array(count + 1);
    for (let index = 0; index < count; index += 1) start[index + 1] = start[index] + this.degree[index];
    const cursor = start.slice(0, count);
    const list = new Int32Array(links.length * 2);
    for (let index = 0; index < links.length; index += 1) {
      list[cursor[this.linkS[index]]++] = this.linkT[index];
      list[cursor[this.linkT[index]]++] = this.linkS[index];
    }
    this.adjStart = start;
    this.adjList = list;

    /* Shared-neighbourhood affinity. Both adjacency runs are built in insertion
     * order, so they are sorted for the intersection scan below. */
    for (let index = 0; index < count; index += 1) {
      const from = start[index];
      const to = start[index + 1];
      if (to - from > 1) list.subarray(from, to).sort();
    }
    const ranked = Array.from({length: count}, (_, index) => this.degree[index]).sort((a, b) => a - b);
    const degreeCap = Math.max(1, ranked[Math.floor(ranked.length * 0.95)] || 1);
    const capLog = Math.log2(degreeCap + 1);
    for (let index = 0; index < count; index += 1) {
      this.degreeNorm[index] = clamp(Math.log2(this.degree[index] + 1) / capLog, 0, 1);
    }

    const mark = new Uint8Array(count);
    for (let index = 0; index < links.length; index += 1) {
      const a = this.linkS[index];
      const b = this.linkT[index];
      for (let slot = start[a]; slot < start[a + 1]; slot += 1) mark[list[slot]] = 1;
      let shared = 0;
      for (let slot = start[b]; slot < start[b + 1]; slot += 1) if (mark[list[slot]]) shared += 1;
      for (let slot = start[a]; slot < start[a + 1]; slot += 1) mark[list[slot]] = 0;
      const smaller = Math.max(1, Math.min(this.degree[a], this.degree[b]) - 1);
      const overlap = clamp(shared / smaller, 0, 1);
      const heaviest = Math.max(this.degreeNorm[a], this.degreeNorm[b]);
      this.linkAffinity[index] = (AFFINITY_MIN + (AFFINITY_MAX - AFFINITY_MIN) * Math.sqrt(overlap))
        * (1 + DEGREE_PULL * heaviest);
    }

    this.activeCount = 0;
    for (let index = 0; index < this.count; index += 1) if (this.awake[index]) this.activeCount += 1;
  }

  /** Current soft radius the aggregate is pressing against. */
  radius() {
    return this.radiusEma > 0 ? this.radiusEma : radiusForCount(this.activeCount);
  }

  /** Put every node back to sleep and forget where it was. */
  reset() {
    this.awake.fill(0);
    this.pinned.fill(0);
    this.settle.fill(0);
    this.integration.fill(0);
    this.ignite.fill(0);
    this.speed.fill(0);
    this.age.fill(0);
    this.linkGain.fill(0);
    this.vx.fill(0); this.vy.fill(0); this.vz.fill(0);
    this.px.fill(0); this.py.fill(0); this.pz.fill(0);
    this.activeCount = 0;
    this.radiusEma = 0;
    this.sinceWake = 0;
    this.containment = 1;
    this.maxSpeed = 0;
    this.coreSpeed = 0;
    this.coreDrift = 0;
  }

  sleep(index: number) {
    if (!this.awake[index]) return;
    this.awake[index] = 0;
    this.activeCount -= 1;
    this.settle[index] = 0;
    this.integration[index] = 0;
    this.ignite[index] = 0;
    this.speed[index] = 0;
    this.age[index] = 0;
    this.vx[index] = 0; this.vy[index] = 0; this.vz[index] = 0;
  }

  /**
   * Light one node somewhere outside the cluster. The direction is mostly
   * random — the silhouette is deliberately incomplete early on — with a small
   * lean toward whatever kin are already out there, which is topology, not a
   * coordinate: it only shortens the journey, it never assigns a seat.
   */
  wake(index: number) {
    if (this.awake[index]) return;
    if (this.radiusEma <= 0) this.radiusEma = radiusForCount(this.activeCount + 1);
    // just outside whatever the cluster currently *is*, not what a formula says
    const radius = this.radiusEma;
    let dx = 0;
    let dy = 0;
    let dz = 0;
    let pull = 0;
    for (let slot = this.adjStart[index]; slot < this.adjStart[index + 1]; slot += 1) {
      const other = this.adjList[slot];
      if (!this.awake[other]) continue;
      dx += this.px[other]; dy += this.py[other]; dz += this.pz[other];
      pull += 1;
    }
    let rx = this.random() * 2 - 1;
    let ry = this.random() * 2 - 1;
    let rz = this.random() * 2 - 1;
    let rl = length3(rx, ry, rz);
    while (rl < 1e-3) {
      rx = this.random() * 2 - 1;
      ry = this.random() * 2 - 1;
      rz = this.random() * 2 - 1;
      rl = length3(rx, ry, rz);
    }
    rx /= rl; ry /= rl; rz /= rl;
    if (pull > 0) {
      const nl = length3(dx, dy, dz);
      if (nl > 1e-4) {
        rx = rx * (1 - SPAWN_BIAS) + (dx / nl) * SPAWN_BIAS;
        ry = ry * (1 - SPAWN_BIAS) + (dy / nl) * SPAWN_BIAS;
        rz = rz * (1 - SPAWN_BIAS) + (dz / nl) * SPAWN_BIAS;
        const bl = length3(rx, ry, rz) || 1;
        rx /= bl; ry /= bl; rz /= bl;
      }
    }
    const shell = radius * (SPAWN_MIN + this.random() * SPAWN_SPAN);
    this.px[index] = rx * shell;
    this.py[index] = ry * shell;
    this.pz[index] = rz * shell;
    this.vx[index] = -rx * SPAWN_INWARD;
    this.vy[index] = -ry * SPAWN_INWARD;
    this.vz[index] = -rz * SPAWN_INWARD;
    this.awake[index] = 1;
    this.activeCount += 1;
    this.sinceWake = 0;
    this.settle[index] = 0;
    this.integration[index] = 0;
    this.ignite[index] = 0;
    this.speed[index] = SPAWN_INWARD;
    this.age[index] = 0;
    this.pinned[index] = 0;
  }

  /**
   * Loosen one node — used when a dragged node is released back into the field.
   * It is deliberately node-local: neighbours are not touched, because nothing
   * in this engine is ever allowed to reheat a region.
   */
  /**
   * Flag the neighbourhood of a node the viewer is holding. Those nodes are
   * allowed to answer the pull at full mobility, and their link budget is
   * widened, so hauling on a node visibly drags what it is joined to instead of
   * that pull being one nineteenth of a force balance that ignores it.
   */
  markHeld(index: number, held: boolean) {
    const flag = held ? 1 : 0;
    for (let slot = this.adjStart[index]; slot < this.adjStart[index + 1]; slot += 1) {
      this.heldNeighbour[this.adjList[slot]] = flag;
    }
  }

  loosen(index: number, strength = 1) {
    if (!this.awake[index]) return;
    const slack = 1 - clamp(strength, 0, 1);
    this.settle[index] = Math.min(this.settle[index], slack);
    // mobility reads membership, so a released node must give that up too,
    // otherwise it would be dropped back into the field unable to react
    this.integration[index] = Math.min(this.integration[index], slack);
  }

  /**
   * The distance at which these two stop closing — their own personal space,
   * sized by how well connected each of them is, exactly as the renderer sizes
   * them. The canvas uses it to decide when a pair counts as having met.
   */
  contactFloor(a: number, b: number) {
    return Math.max(CONTACT_MIN, CONTACT_MARGIN * (this.bodyRadius[a] + this.bodyRadius[b]));
  }

  /**
   * The distance at which these two actually come to rest against each other,
   * *including* the extra squeeze a pair gets while it is hauling itself
   * together. This — not the nominal floor — is what "they have met" has to be
   * measured against, and it is the contact floor and nothing else. There is
   * no version of this that depends on whether a pair happens to want a
   * connection: any discount here is a line drawn before contact, and
   * "they collided" has to mean the same thing for every pair or it means
   * nothing at all.
   */
  meetingDistance(a: number, b: number) {
    return this.contactFloor(a, b);
  }

  /** Everything settled enough to be considered part of the finished globe. */
  settledCount(threshold = 0.6) {
    let total = 0;
    for (let index = 0; index < this.count; index += 1) {
      if (this.awake[index] && this.settle[index] >= threshold) total += 1;
    }
    return total;
  }

  /**
   * One fixed simulation step. `dt` is always small and constant — the caller
   * substeps — so the integrator never has to cope with a surprise.
   */
  step(dt: number, tuning: FieldTuning) {
    const count = this.count;
    if (count === 0) return;
    const relaxing = this.relaxing;
    const {px, py, pz, vx, vy, vz, fx, fy, fz, lx, ly, lz, rx, ry, rz, awake} = this;

    fx.fill(0, 0, count); fy.fill(0, 0, count); fz.fill(0, 0, count);
    lx.fill(0, 0, count); ly.fill(0, 0, count); lz.fill(0, 0, count);
    rx.fill(0, 0, count); ry.fill(0, 0, count); rz.fill(0, 0, count);

    /* The containment follows the cloud: it eases toward the radius the members
     * are actually filling, so it sits just outside the surface whatever the
     * forces settle on, and because it only ever eases, waking a node can never
     * step the boundary. */
    this.sinceWake += dt;
    this.containment = 1;
    if (this.radiusEma <= 0) this.radiusEma = radiusForCount(this.activeCount);
    const radius = this.radiusEma;

    /* ---------------------------------------------------- neighbourhood grid */
    const cell = REPEL_CUTOFF;
    const head = this.head;
    const next = this.next;
    head.fill(-1);
    const hash = (cx: number, cy: number, cz: number) =>
      (((cx * 73856093) ^ (cy * 19349663) ^ (cz * 83492791)) & this.hashMask) >>> 0;
    for (let index = 0; index < count; index += 1) {
      if (!awake[index]) continue;
      const key = hash(
        Math.floor(px[index] / cell),
        Math.floor(py[index] / cell),
        Math.floor(pz[index] / cell),
      );
      next[index] = head[key];
      head[key] = index;
    }

    /* -------------------------------------------- repulsion + collision relief */
    const repelK = REPEL_BASE * Math.max(0, tuning.repel);
    const cutoff2 = REPEL_CUTOFF * REPEL_CUTOFF;
    for (let index = 0; index < count; index += 1) {
      if (!awake[index]) continue;
      const ax = px[index];
      const ay = py[index];
      const az = pz[index];
      const cx = Math.floor(ax / cell);
      const cy = Math.floor(ay / cell);
      const cz = Math.floor(az / cell);
      let sx = 0;
      let sy = 0;
      let sz = 0;
      for (let ox = -1; ox <= 1; ox += 1) {
        for (let oy = -1; oy <= 1; oy += 1) {
          for (let oz = -1; oz <= 1; oz += 1) {
            for (let other = head[hash(cx + ox, cy + oy, cz + oz)]; other >= 0; other = next[other]) {
              if (other <= index) continue;
              let dx = px[other] - ax;
              let dy = py[other] - ay;
              let dz = pz[other] - az;
              let d2 = dx * dx + dy * dy + dz * dz;
              if (d2 > cutoff2) continue;
              if (d2 < 1e-8) {
                // exact overlap has no direction — take a deterministic one
                dx = 1e-3; dy = 0; dz = 0;
                d2 = 1e-6;
              }
              const distance = Math.sqrt(d2);
              let force = repelK / (d2 + REPEL_SOFT);
              // let the last gap close: crowd pressure fades out at contact range
              if (distance < BOND_AT) force *= Math.pow(distance / BOND_AT, BOND_EXP);
              // ...and only genuine crowding of the drawn bodies is pushed back.
              // One contact distance, identical for every pair, always.
              const touchAt = this.contactFloor(index, other);
              if (distance < touchAt) force += COLLIDE_K * (touchAt - distance);
              if (force > MAX_PAIR_FORCE) force = MAX_PAIR_FORCE;
              force *= 1 - distance / REPEL_CUTOFF;
              const scale = force / distance;
              const ux = dx * scale;
              const uy = dy * scale;
              const uz = dz * scale;
              sx -= ux; sy -= uy; sz -= uz;
              fx[other] += ux; fy[other] += uy; fz[other] += uz;
            }
          }
        }
      }
      fx[index] += sx; fy[index] += sy; fz[index] += sz;
    }

    /* -------------------------------------------- link attraction, as revealed
     * A link pulls exactly as hard as its line is drawn. `linkGain` is written
     * by the renderer from the same slow progress the edge is growing with, so
     * tension climbs from zero while the viewer watches the connection appear
     * and the two endpoints visibly answer each other. A link nobody can see
     * yet contributes nothing — not to the force, not to the hub softening.
     *
     * The pull itself has no target: `d / (d + LINK_SOFT)` is positive for
     * every d > 0, rises smoothly, and saturates. It never reaches zero and it
     * never changes sign, so no distance is singled out as "the" distance for a
     * connection. Where a pair actually settles is decided elsewhere — by the
     * other links each endpoint is answering, by the crowd around them, and by
     * the containment — and it keeps moving as those change. */
    const linkK = LINK_BASE * Math.max(0, tuning.linkStrength);
    const linkLoad = this.linkLoad;
    const shownLoad = this.shownLoad;
    linkLoad.fill(0, 0, count);
    shownLoad.fill(0, 0, count);
    let weakestPull = Infinity;
    let closestLinked = Infinity;
    for (let index = 0; index < this.linkCount; index += 1) {
      const s = this.linkS[index];
      const t = this.linkT[index];
      if (!awake[s] || !awake[t]) continue;
      // gains above 1 exist so a held node can haul on its connections
      const gain = relaxing ? 1 : clamp(this.linkGain[index], 0, DRAG_GAIN_MAX);
      if (gain <= 0) continue;
      linkLoad[s] += gain; linkLoad[t] += gain;
      const drawn = relaxing ? 1 : Math.min(1, this.linkShown[index]);
      shownLoad[s] += drawn; shownLoad[t] += drawn;
      const dx = px[t] - px[s];
      const dy = py[t] - py[s];
      const dz = pz[t] - pz[s];
      const distance = length3(dx, dy, dz) || 1e-4;
      if (distance < this.linkMinSpan[index]) this.linkMinSpan[index] = distance;
      /* A group's weight counts when it is hauling something in, and only
       * then. Applying it to every link as well would just squeeze the finished
       * structure — once the whole graph is one group, every connection would
       * pull three times harder and the ball would collapse in on itself. */
      const reaching = !relaxing && this.linkReach[index] === 1;
      const mass = reaching
        ? 1 + GROUP_PULL * Math.max(this.groupMass[s], this.groupMass[t])
        : 1;
      let force = linkK * this.linkAffinity[index] * mass * (distance / (distance + LINK_SOFT));
      if (force < weakestPull) weakestPull = force;
      if (distance < closestLinked) closestLinked = distance;
      const ceiling = (this.pinned[s] || this.pinned[t] || reaching)
        ? MAX_LINK_FORCE * 4
        : MAX_LINK_FORCE;
      if (force > ceiling) force = ceiling;
      const scale = (force * gain) / distance;
      if (reaching) {
        rx[s] += dx * scale; ry[s] += dy * scale; rz[s] += dz * scale;
        rx[t] -= dx * scale; ry[t] -= dy * scale; rz[t] -= dz * scale;
      } else {
        lx[s] += dx * scale; ly[s] += dy * scale; lz[s] += dz * scale;
        lx[t] -= dx * scale; ly[t] -= dy * scale; lz[t] -= dz * scale;
      }
    }
    this.minLinkForce = Number.isFinite(weakestPull) ? weakestPull : 0;
    this.closestActiveLink = Number.isFinite(closestLinked) ? closestLinked : 0;

    /* ------------------------------ boundary skin, recentering pull, integrate */
    const centerK = CENTER_BASE * Math.max(0, tuning.center);
    const skin = radius * (1 + BOUNDARY_SOFT);
    const speedBlend = 1 - Math.exp(-dt / SPEED_EMA_TAU);
    const riseBlend = 1 - Math.exp(-dt / SETTLE_RISE_TAU);
    const fallBlend = 1 - Math.exp(-dt / SETTLE_FALL_TAU);
    const joinRise = 1 - Math.exp(-dt / INTEGRATE_RISE_TAU);
    const joinFall = 1 - Math.exp(-dt / INTEGRATE_FALL_TAU);
    const igniteStep = dt / IGNITE_SECONDS;
    this.flowClock += dt;
    const flowT = this.flowClock * FLOW_HZ * Math.PI * 2;
    const stir = relaxing ? 0 : clamp(this.stir, 0, 1);
    /* Scaled by the size the node count implies, never by the measured cloud.
     * Measuring the thing the current is pushing on and then pushing harder
     * because it got bigger is a feedback loop, and it runs away. */
    const flowAmp = FLOW_SPEED * radiusForCount(this.activeCount) * stir * dt;
    const flowScale = (2 * Math.PI * FLOW_WAVES) / Math.max(0.5, radius);
    const swirlT = this.flowClock * SWIRL_DRIFT;
    let sxAxis = Math.sin(swirlT * 0.73);
    let syAxis = Math.cos(swirlT * 0.51) * 1.4;
    let szAxis = Math.sin(swirlT * 0.37 + 1.3);
    const axisLen = length3(sxAxis, syAxis, szAxis) || 1;
    sxAxis /= axisLen; syAxis /= axisLen; szAxis /= axisLen;
    const swirl = SWIRL_RATE * stir * dt;
    const stepCap = relaxing ? STEP_CAP * 10 : STEP_CAP;
    const accCap = relaxing ? ACC_CAP * 4 : ACC_CAP;
    const speedCap = relaxing ? SPEED_CAP * 9 : SPEED_CAP;
    let cx = 0;
    let cy = 0;
    let cz = 0;
    let cw = 0;
    let coreSum = 0;
    let coreN = 0;
    let peak = 0;
    let spreadSq = 0;
    let spreadW = 0;

    for (let index = 0; index < count; index += 1) {
      if (!awake[index]) continue;
      this.age[index] += dt;
      // light begins at creation and is never taken away
      if (this.ignite[index] < 1) this.ignite[index] = Math.min(1, this.ignite[index] + igniteStep);

      // a hub answers to the average of its *live* links, so a node with two
      // connections so far feels both of them properly
      const soften = 1 / (1 + LINK_DEG_SOFT * this.linkLoad[index]);
      let tx = lx[index] * soften;
      let ty = ly[index] * soften;
      let tz = lz[index] * soften;
      const linkTotal = length3(tx, ty, tz);
      const totalCap = this.heldNeighbour[index] ? MAX_LINK_TOTAL * 4 : MAX_LINK_TOTAL;
      if (linkTotal > totalCap) {
        const scale = totalCap / linkTotal;
        tx *= scale; ty *= scale; tz *= scale;
      }
      // the reach is added whole, never averaged into the crowd of the rest
      let ax = fx[index] + tx + rx[index];
      let ay = fy[index] + ty + ry[index];
      let az = fz[index] + tz + rz[index];

      const r = length3(px[index], py[index], pz[index]);
      if (r > 1e-4) {
        const ux = px[index] / r;
        const uy = py[index] / r;
        const uz = pz[index] / r;
        // a node nothing has caught yet barely feels the middle, so it stays out
        const bound = clamp(this.shownLoad[index] / WIRED_FULL, 0, 1);
        const pull = RIM_FREEDOM + (1 - RIM_FREEDOM) * bound;
        let inward = Math.min(CENTER_MAX, centerK * (r / radius)) * pull;
        if (r > skin) {
          const over = (r - skin) / radius;
          inward += Math.min(BOUNDARY_MAX, BOUNDARY_K * over);
        }
        ax -= ux * inward;
        ay -= uy * inward;
        az -= uz * inward;
      }

      const rest = this.settle[index];
      const joined = this.integration[index];
      const weight = 0.04 + joined * joined;
      // members define where the surface is; travellers still outside do not
      spreadSq += (px[index] * px[index] + py[index] * py[index] + pz[index] * pz[index]) * joined;
      spreadW += joined;

      if (this.pinned[index]) {
        this.vx[index] = 0; this.vy[index] = 0; this.vz[index] = 0;
        this.speed[index] = SETTLE_SPEED_HI;
        this.settle[index] += (0 - rest) * fallBlend;
        // a held node is still a member: its links stay live while it is moved
        this.integration[index] += (1 - this.integration[index]) * joinRise;
        cx += px[index] * weight; cy += py[index] * weight; cz += pz[index] * weight;
        cw += weight;
        continue;
      }

      // mass only counts once a node belongs: a newcomer still crosses the gap
      // at its own pace however many connections are waiting for it
      // whatever the viewer is hauling on gets to answer properly
      const held = this.heldNeighbour[index] === 1;
      const mob = relaxing || held
        ? 1
        : (MOB_MIN + (1 - MOB_MIN) * Math.pow(1 - joined, MOB_EXP))
          / (1 + HUB_MASS * this.degreeNorm[index] * joined);
      const accel = length3(ax, ay, az);
      if (accel > accCap) {
        const scale = accCap / accel;
        ax *= scale; ay *= scale; az *= scale;
      }

      let nvx = vx[index] + ax * mob * dt;
      let nvy = vy[index] + ay * mob * dt;
      let nvz = vz[index] + az * mob * dt;
      const tau = DAMP_TAU + (DAMP_TAU_SETTLED - DAMP_TAU) * joined;
      const damp = Math.exp(-dt / tau);
      nvx *= damp; nvy *= damp; nvz *= damp;
      const speed = length3(nvx, nvy, nvz);
      const cap = (held ? SPEED_CAP * 3 : speedCap) * mob;
      if (speed > cap && speed > 1e-6) {
        const scale = cap / speed;
        nvx *= scale; nvy *= scale; nvz *= scale;
      }

      // absolute displacement clamp: no configuration of forces, sliders or
      // timestep can move a node further than this in one step
      let dx = nvx * dt;
      let dy = nvy * dt;
      let dz = nvz * dt;
      const disp = length3(dx, dy, dz);
      if (disp > stepCap) {
        const scale = stepCap / disp;
        dx *= scale; dy *= scale; dz *= scale;
        nvx *= scale; nvy *= scale; nvz *= scale;
      }
      vx[index] = nvx; vy[index] = nvy; vz[index] = nvz;

      /* The current: the curl of a potential, so divergence-free — it stirs the
       * cluster without ever compressing or inflating any part of it — and
       * smooth in space, so neighbours are carried together.
       *
       * Each component is a *difference* of two waves rather than a single one.
       * With one wave per axis the three components share their zeros, and
       * whenever they line up the current vanishes everywhere at once: the
       * cluster visibly stalls, then picks up again. Two out-of-phase waves per
       * axis have no common zero, so the speed stays even. */
      /* Two nodes on their way to meet each other read the current at the point
       * midway between them rather than each at its own position, so both ends
       * get the identical displacement and the stirring carries them as one
       * body. Without this the pair is simply sheared apart: the current is not
       * throttled by any of the caps above — it is added to the position after
       * all of them — and at this wavelength two nodes a spacing apart already
       * sit in opposite lobes, so it pulls them apart several times faster than
       * either is permitted to move toward the other. No amount of extra force
       * can win against that, because the limiter is a speed cap. */
      let sampleX = px[index];
      let sampleY = py[index];
      let sampleZ = pz[index];
      const mate = this.reachPartner[index];
      if (mate >= 0 && awake[mate] && this.reachPartner[mate] === index) {
        sampleX = (sampleX + px[mate]) * 0.5;
        sampleY = (sampleY + py[mate]) * 0.5;
        sampleZ = (sampleZ + pz[mate]) * 0.5;
      }

      if (flowAmp > 0) {
        const kx = sampleX * flowScale;
        const ky = sampleY * flowScale;
        const kz = sampleZ * flowScale;
        dx += (Math.cos(ky + flowT * 1.31) - Math.cos(kz + flowT * 0.70)) * flowAmp;
        dy += (Math.cos(kz + flowT * 0.83) - Math.cos(kx + flowT * 1.17)) * flowAmp;
        dz += (Math.cos(kx + flowT * 1.09) - Math.cos(ky + flowT * 0.61)) * flowAmp;
      }

      /* ...and the slow turn of the whole ball: omega x r, applied to everyone
       * alike, so nothing inside it moves relative to anything else. */
      if (swirl > 0) {
        const shell = clamp(length3(sampleX, sampleY, sampleZ) / radius, 0, 1.4);
        const spin = swirl * (1 + SWIRL_SHEAR * (shell - 0.5));
        dx += (syAxis * sampleZ - szAxis * sampleY) * spin;
        dy += (szAxis * sampleX - sxAxis * sampleZ) * spin;
        dz += (sxAxis * sampleY - syAxis * sampleX) * spin;
      }

      px[index] += dx;
      py[index] += dy;
      pz[index] += dz;

      const moving = length3(nvx, nvy, nvz);
      if (moving > peak) peak = moving;
      this.speed[index] += (moving - this.speed[index]) * speedBlend;

      /* A node is settled when it is slow, old enough, and actually inside the
       * ball. Travelling newcomers never qualify, however calm they look. */
      const calm = clamp((SETTLE_SPEED_HI - this.speed[index]) / (SETTLE_SPEED_HI - SETTLE_SPEED_LO), 0, 1);
      const grown = clamp((this.age[index] - MIN_SETTLE_AGE) / MIN_SETTLE_AGE, 0, 1);
      const nowR = length3(px[index], py[index], pz[index]);
      const inside = clamp((radius * (INSIDE_SLACK + INSIDE_SPAN) - nowR) / (radius * INSIDE_SPAN), 0, 1);
      const target = calm * grown * inside;
      const blend = target > this.settle[index] ? riseBlend : fallBlend;
      this.settle[index] += (target - this.settle[index]) * blend;

      /* Membership, independent of how busy the node currently is. */
      const belongs = grown * inside;
      this.integration[index] +=
        (belongs - this.integration[index]) * (belongs > this.integration[index] ? joinRise : joinFall);

      if (joined >= CORE_REST) { coreSum += moving; coreN += 1; }
      cx += px[index] * weight; cy += py[index] * weight; cz += pz[index] * weight;
      cw += weight;
    }

    /* The surface, measured. rms radius of a uniform ball is sqrt(3/5) R, so
     * inverting that gives the radius the members are actually filling. */
    if (spreadW > 1e-6) {
      const measured = Math.sqrt(spreadSq / spreadW) * 1.291;
      const target = Math.max(measured, radiusForCount(this.activeCount) * MIN_FILL, 1);
      const ease = relaxing ? 1 : 1 - Math.exp(-dt / RADIUS_TAU);
      this.radiusEma += (target - this.radiusEma) * ease;
    }

    /* Total-energy governor. The settled core has a mean-speed ceiling: if the
     * field ever tries to spend more than that, every core velocity is scaled
     * back in the same step. The cluster therefore cannot surge, whatever is
     * happening at its surface. */
    const coreMean = coreN > 0 ? coreSum / coreN : 0;
    this.coreSpeed = coreMean;
    this.maxSpeed = peak;
    if (!relaxing && coreN > 0 && coreMean > CORE_SPEED_BUDGET) {
      const scale = CORE_SPEED_BUDGET / coreMean;
      for (let index = 0; index < count; index += 1) {
        if (!awake[index] || this.pinned[index]) continue;
        if (this.integration[index] < CORE_REST) continue;
        vx[index] *= scale; vy[index] *= scale; vz[index] *= scale;
      }
    }

    /* Recentering is driven by the *settled core* and is rate-limited. A node
     * appearing far outside carries almost no weight here, so its arrival can
     * never counter-translate the cluster the viewer is looking at. */
    this.coreDrift = 0;
    if (cw > 0) {
      cx /= cw; cy /= cw; cz /= cw;
      const drift = length3(cx, cy, cz);
      this.coreDrift = drift;
      if (drift > 1e-6) {
        const blend = relaxing ? 1 : 1 - Math.exp(-dt / RECENTER_TAU);
        let shift = drift * blend;
        const maxShift = relaxing ? drift : RECENTER_MAX * dt;
        if (shift > maxShift) shift = maxShift;
        const kx = (cx / drift) * shift;
        const ky = (cy / drift) * shift;
        const kz = (cz / drift) * shift;
        for (let index = 0; index < count; index += 1) {
          if (!awake[index]) continue;
          px[index] -= kx; py[index] -= ky; pz[index] -= kz;
        }
      }
    }
  }

  /**
   * Run the field forward until it stops moving — used for the reduced-motion
   * path, which needs a stable picture immediately and no animation at all.
   * Relaxation lifts the slow-motion budget: it happens between two frames, so
   * nobody ever sees the speed it runs at.
   */
  relax(steps: number, dt: number, tuning: FieldTuning) {
    this.relaxing = true;
    try {
      for (let step = 0; step < steps; step += 1) this.step(dt, tuning);
    } finally {
      this.relaxing = false;
    }
    for (let index = 0; index < this.count; index += 1) {
      if (!this.awake[index]) continue;
      this.vx[index] = 0; this.vy[index] = 0; this.vz[index] = 0;
      this.speed[index] = 0;
      this.settle[index] = 1;
      this.integration[index] = 1;
      this.ignite[index] = 1;
      this.age[index] = MIN_SETTLE_AGE * 4;
    }
    this.maxSpeed = 0;
    this.coreSpeed = 0;
  }
}
