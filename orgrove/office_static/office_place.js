// Where everyone in the 3D office is, from the real state alone (#70, #102, #121). Every look (classic, new)
// asks this module and only draws the answer, so the same snapshot puts each person in the same place.
// Pure: no DOM, no three.js (tests/test_office_place.py runs it under node).

export const SLOT_MS = 20 * 60000;                      // a table-tennis game
export const hashStr = (t) => [...t].reduce((h, c) => (h * 33 + c.charCodeAt(0)) >>> 0, 5381);

// Table tennis (#121): role + 20-minute slot picks who plays, the same answer for every viewer and every reload.
// A game is the one thing that runs the whole slot, and never with a lone player.
function tableTennis(roles, spots, now) {
  const slot = Math.floor(now / SLOT_MS), used = new Map(), out = new Map();
  for (const role of roles) {
    let i = hashStr(role + slot) % spots.length;
    for (let k = 0; k < spots.length; k++, i = (i + 1) % spots.length) {
      const n = used.get(i) || 0;
      if (n < spots[i].places.length) { used.set(i, n + 1); out.set(role, i); break; }
    }
  }
  const tt = spots.findIndex((s) => s.min);
  if (tt < 0) return [];
  const at = roles.filter((r) => out.get(r) === tt);
  if (at.length === 1) {                                   // pull a partner over, else nobody plays alone
    const partner = roles.find((r) => out.get(r) !== tt);
    return partner ? [at[0], partner] : [];
  }
  return at.slice(0, spots[tt].places.length);
}

// Everything else on a break (owner, 2026-10-30: "not normal human will do" 20 minutes at a window): a few
// minutes at one thing, a short stroll, then something different. Minutes per visit:
export const STAY_MIN = { "coffee machine": [2, 4], window: [1, 2], bookshelf: [3, 5], sofa: [3, 8], stroll: [0.5, 1], cabin: [3, 6] };
const CHOICES = [["coffee machine", 1], ["window", 1], ["bookshelf", 1], ["sofa", 2]];
const OWN_CABIN = [...CHOICES, ["cabin", 3]];             // the CTO and Finance drop by their own cabin now and then
const PLAN_MS = 6 * 3600000;                              // a plan starts afresh every 6 hours: a short replay
const rng = (seed) => { let s = seed >>> 0 || 1; return () => (s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296; };
function pick(choices, r) {
  const total = choices.reduce((n, [, w]) => n + w, 0);
  let x = r() * total;
  for (const [name, w] of choices) if ((x -= w) < 0) return name;
  return choices[choices.length - 1][0];
}

// What `role` is doing at `now`: a pure function of the role and the clock (every viewer, every reload agree).
// Activities alternate with strolls, and the next activity is never the one just left.
export function activityAt(role, now, own = false) {
  const t0 = Math.floor(now / PLAN_MS) * PLAN_MS, r = rng(hashStr(role + t0)), choices = own ? OWN_CABIN : CHOICES;
  let t = t0, prev = null, walk = false;
  for (;;) {
    const act = walk ? "stroll" : pick(choices.filter(([n]) => n !== prev), r);
    const [lo, hi] = STAY_MIN[act], until = t + (lo + (hi - lo) * r()) * 60000;
    if (now < until) return { act, since: t, until, prev };
    if (!walk) prev = act;
    t = until; walk = !walk;
  }
}

// Break places for everyone resting. `own` roles (the CTO, Finance) may visit their cabin ({ cabin: true }).
// A spot that is full sends the person to their next choice; a role with no place left goes to the lounge.
export function breakPlaces(roles, spots, now = Date.now(), own = []) {
  const out = new Map(), used = new Map(), byName = new Map(spots.map((s) => [s.name, s]));
  const take = (role, spot) => {
    const n = used.get(spot.name) || 0;
    if (n >= spot.places.length) return false;
    used.set(spot.name, n + 1); out.set(role, { spot, place: spot.places[n], index: n });
    return true;
  };
  const tt = spots.find((s) => s.min);
  for (const role of tableTennis(roles, spots, now)) take(role, tt);
  for (const role of [...roles, ...own]) {
    if (out.has(role)) continue;
    const { act } = activityAt(role, now, own.includes(role));
    if (act === "cabin") { out.set(role, { cabin: true }); continue; }
    const order = [act, ...CHOICES.map(([n]) => n).filter((n) => n !== act), "stroll"];
    order.some((name) => byName.has(name) && take(role, byName.get(name)));
  }
  return out;
}

// Meeting seats (#102): seat 0 is the CTO's, facing the agenda; the others in the order they were invited.
export function seatOf(character, mtg) {
  if (!mtg?.active) return -1;
  if (character === "cto") return 0;
  const order = [...new Set((mtg.participants || []).map((x) => mtg.characters[x]))];
  const i = order.indexOf(character);
  return i < 0 ? -1 : 1 + i;
}

// Everyone's place. `layout` gives the look's break spots, how many lounge seats it has and, optionally, who is
// always in the office though not in the roster (`always`: the CTO, and Finance in ORGrove Pro).
// A place is { where: "meeting" | "desk" | "break" | "lounge" | "home", index, spot, place }.
export function placements(s, mtg, layout, now = Date.now()) {
  const roster = s.roster || [];
  const resting = roster.filter((r) => r.status !== "working" && seatOf(r.role, mtg) < 0);
  // The CTO works while something waits on them (the inbox); Finance has no queue of its own. Free, they take
  // breaks like everyone else and drop by their own cabin now and then (owner, 2026-10-30).
  const always = layout.always || ["cto", "finance"];
  const free = always.filter((who) => seatOf(who, mtg) < 0 && !(who === "cto" && (s.inbox || []).length));
  const brk = breakPlaces(resting.map((r) => r.role), layout.breakSpots, now, free);
  const at = new Map();
  let lounge = 0;
  for (const row of roster) {
    const seat = seatOf(row.role, mtg), working = row.status === "working", task = row.on?.[0];
    let p;
    if (seat >= 0 && !working) p = { where: "meeting", index: seat };          // invited and free: walks to the meeting room
    else if (working) p = { where: "desk" };          // QA and Security review at their own desks too (no review room)
    else if (brk.get(row.role)?.spot) { const b = brk.get(row.role); p = { where: "break", spot: b.spot.name, index: b.index, place: b.place }; }
    else p = { where: "lounge", index: lounge++ % layout.loungeSeats };
    at.set(row.role, { ...p, working, seat, task, joinsLater: seat >= 0 && working,   // honest: a live run is finished first
                       breakSpot: !working && seat < 0 ? brk.get(row.role)?.spot?.name ?? null : null });
  }
  for (const who of always) {                                                     // not in the roster: always present
    const seat = seatOf(who, mtg), b = brk.get(who);
    at.set(who, seat >= 0 ? { where: "meeting", index: seat, seat }
      : b?.spot ? { where: "break", spot: b.spot.name, index: b.index, place: b.place, seat, breakSpot: b.spot.name }
      : { where: "home", seat });
  }
  return { at, resting: resting.length,
           pingpong: [...brk].filter(([, b]) => b.spot?.name === "table tennis").map(([role]) => role) };
}
