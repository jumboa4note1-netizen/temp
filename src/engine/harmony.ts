/* ------------------------------------------------------------------ *
 *  Fahh MIDI · key & scale estimation engine
 *
 *  Pure key / scale analysis (global + smooth local-key follower)
 *  using combined Temperley + Krumhansl-Schmuckler pitch-class profiles,
 *  bass & cadential tonic weighting, and canonical piano enharmonics.
 *  Zero chord-recognition or banner logic.
 * ------------------------------------------------------------------ */

import type { FilenameKeyHint } from "./metadata";

export interface KeyNote {
  midi: number;
  start: number;
  end: number;
  vel: number;
}

export interface KeyEstimate {
  tonic: number; // pitch class 0..11
  minor: boolean;
  sharps: number; // circle-of-fifths position used for spelling
  name: string; // e.g. "E♭ minor"
  confidence: number; // 0..1
  source: "analysis" | "key-signature";
}

export interface KeyModel {
  global: KeyEstimate;
  bins: Float32Array[]; // 1s bins of weighted pitch classes
  prefix: Float32Array[]; // prefix sums per pitch class for O(1) window queries
  localBins: Map<number, { tonic: number; minor: boolean; sharps: number; name: string }>;
  allowLocal: boolean;
}

const SHARP_NAMES = ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"];
const FLAT_NAMES = ["C", "D♭", "D", "E♭", "E", "F", "G♭", "G", "A♭", "A", "B♭", "B"];
const KEY0_NAMES = ["C", "C♯", "D", "E♭", "E", "F", "F♯", "G", "A♭", "A", "B♭", "B"];

// Canonical piano key signatures for major and minor modes (e.g. Eb minor (-6) rather than D# minor, Db major (-5) rather than C# major)
const MAJOR_SHARPS = [0, -5, 2, -3, 4, -1, 6, 1, -4, 3, -2, 5];
const MINOR_SHARPS = [-3, 4, -1, -6, 1, -4, 3, -2, 5, 0, -5, 2];

const mod12 = (n: number) => ((n % 12) + 12) % 12;

export function pcName(pc: number, sharps: number): string {
  pc = mod12(pc);
  return (sharps > 0 ? SHARP_NAMES : sharps < 0 ? FLAT_NAMES : KEY0_NAMES)[pc];
}

export function keySharps(tonic: number, minor: boolean): number {
  const t = mod12(tonic);
  return minor ? MINOR_SHARPS[t] : MAJOR_SHARPS[t];
}

export function keyName(tonic: number, minor: boolean, explicitSharps?: number): string {
  const sh = explicitSharps ?? keySharps(tonic, minor);
  return `${pcName(tonic, sh)} ${minor ? "minor" : "major"}`;
}

// Temperley (CBMS) & Krumhansl-Schmuckler pitch-class key profiles
const T_MAJOR = [5.0, 2.0, 3.5, 2.0, 4.5, 4.0, 2.0, 4.5, 2.0, 3.5, 1.5, 4.0];
const T_MINOR = [5.0, 2.0, 3.5, 4.5, 2.0, 4.0, 2.0, 4.5, 3.5, 2.0, 1.5, 4.0];
const KS_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88];
const KS_MINOR = [6.33, 2.68, 3.52, 5.38, 2.6, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17];

function pearson(x: ArrayLike<number>, y: ArrayLike<number>): number {
  let mx = 0;
  let my = 0;
  for (let i = 0; i < 12; i++) {
    mx += x[i];
    my += y[i];
  }
  mx /= 12;
  my /= 12;
  let sxy = 0;
  let sxx = 0;
  let syy = 0;
  for (let i = 0; i < 12; i++) {
    const a = x[i] - mx;
    const b = y[i] - my;
    sxy += a * b;
    sxx += a * a;
    syy += b * b;
  }
  return sxx > 1e-9 && syy > 1e-9 ? sxy / Math.sqrt(sxx * syy) : 0;
}

function scoreHistogram(hist: ArrayLike<number>, cadence?: ArrayLike<number>): { tonic: number; minor: boolean; r: number }[] {
  const out: { tonic: number; minor: boolean; r: number }[] = [];
  const rotT = new Array<number>(12);
  const rotK = new Array<number>(12);
  for (let tonic = 0; tonic < 12; tonic++) {
    for (const minor of [false, true]) {
      const tp = minor ? T_MINOR : T_MAJOR;
      const kp = minor ? KS_MINOR : KS_MAJOR;
      for (let i = 0; i < 12; i++) {
        const idx = mod12(i - tonic);
        rotT[i] = tp[idx];
        rotK[i] = kp[idx];
      }
      let r = 0.6 * pearson(hist, rotT) + 0.4 * pearson(hist, rotK);
      if (cadence) r = 0.85 * r + 0.15 * pearson(cadence, rotT);
      out.push({ tonic, minor, r });
    }
  }
  return out;
}

const isRelative = (a: { tonic: number; minor: boolean }, b: { tonic: number; minor: boolean }) =>
  a.minor !== b.minor && (a.minor ? mod12(a.tonic + 3) === b.tonic : mod12(b.tonic + 3) === a.tonic);

export function buildKeyModel(
  notes: KeyNote[],
  fileKey: { sharps: number; minor: boolean } | null,
  allowLocal: boolean,
  leafHint: FilenameKeyHint | null = null,
): KeyModel {
  let dur = 1;
  for (const n of notes) dur = Math.max(dur, n.end);
  const secs = Math.ceil(dur) + 2;
  const bins: Float32Array[] = Array.from({ length: secs }, () => new Float32Array(12));
  const cadence = new Float32Array(12);

  for (const n of notes) {
    const velW = 0.5 + 0.5 * (n.vel / 127);
    const d = Math.max(0.05, Math.min(n.end - n.start, 3));
    const effD = Math.sqrt(d);
    const bassBoost = n.midi < 54 ? 1.4 : 1.0;
    const pc = mod12(n.midi);
    const wRate = (effD / d) * velW * bassBoost;
    let t = n.start;
    const tEnd = n.start + d;
    while (t < tEnd - 1e-9) {
      const s = Math.min(secs - 1, Math.max(0, Math.floor(t)));
      const seg = Math.min(tEnd, s + 1) - t;
      bins[s][pc] += seg * wRate;
      t += seg > 0 ? seg : 1;
    }
    if (n.start < 10 || n.end > dur - 8) {
      cadence[pc] += effD * velW * (n.midi < 54 ? 1.65 : 1.0);
    }
  }

  const total = new Float32Array(12);
  for (const b of bins) for (let i = 0; i < 12; i++) total[i] += b[i];

  const scores = scoreHistogram(total, cadence);
  if (fileKey) {
    const majTonic = mod12(7 * fileKey.sharps);
    const relMinTonic = mod12(majTonic + 9);
    for (const s of scores) {
      if ((!s.minor && s.tonic === majTonic) || (s.minor && s.tonic === relMinTonic)) s.r += 0.04;
      if (fileKey.minor && s.minor && s.tonic === relMinTonic) s.r += 0.025;
    }
  }
  if (leafHint) {
    for (const s of scores) {
      if (s.tonic === leafHint.tonic && s.minor === leafHint.minor) s.r += 0.035;
    }
  }

  scores.sort((a, b) => b.r - a.r);
  const best = scores[0];
  const second = scores.find((s) => !(s.tonic === best.tonic && s.minor === best.minor) && !isRelative(best, s)) ?? scores[1];
  const conf = Math.max(0, Math.min(1, (best.r - 0.15) * 1.05 + Math.min(0.22, (best.r - second.r) * 1.4)));
  const sharps = keySharps(best.tonic, best.minor);

  const global: KeyEstimate = {
    tonic: best.tonic,
    minor: best.minor,
    sharps,
    name: keyName(best.tonic, best.minor, sharps),
    confidence: conf,
    source: fileKey && Math.abs(fileKey.sharps) > 0 && conf < 0.35 ? "key-signature" : "analysis",
  };

  const prefix: Float32Array[] = [];
  for (let i = 0; i < 12; i++) {
    const p = new Float32Array(secs + 1);
    for (let s = 0; s < secs; s++) p[s + 1] = p[s] + bins[s][i];
    prefix.push(p);
  }

  return { global, bins, prefix, localBins: new Map(), allowLocal };
}

function contextualSharps(tonic: number, minor: boolean, parentSharps: number): number {
  let sh = keySharps(tonic, minor);
  if (parentSharps < 0) {
    if (!minor && tonic === 6) sh = -6; // Gb major instead of F# major in flat keys
    if (!minor && tonic === 1) sh = -5;
    if (minor && tonic === 3) sh = -6; // Eb minor
    if (minor && tonic === 8) sh = -7; // Ab minor
  } else if (parentSharps > 0) {
    if (!minor && tonic === 6) sh = 6; // F# major
    if (minor && tonic === 3) sh = 6; // D# minor instead of Eb minor in sharp keys
    if (minor && tonic === 8) sh = 5; // G# minor
  }
  return sh;
}

function rawLocalKey(m: KeyModel, bin: number): { tonic: number; minor: boolean; sharps: number; name: string } {
  const hit = m.localBins.get(bin);
  if (hit) return hit;

  const center = bin * 3;
  const a = Math.max(0, center - 7);
  const b = Math.min(m.bins.length, center + 10);
  const hist = new Float32Array(12);
  let tot = 0;
  for (let i = 0; i < 12; i++) {
    hist[i] = m.prefix[i][b] - m.prefix[i][a];
    tot += hist[i];
  }

  const g = m.global;
  let res = { tonic: g.tonic, minor: g.minor, sharps: g.sharps, name: g.name };
  if (tot > 2.5) {
    const sc = scoreHistogram(hist);
    let best = sc[0];
    let gScore = -1;
    for (const s of sc) {
      if (s.r > best.r) best = s;
      if (s.tonic === g.tonic && s.minor === g.minor) gScore = s.r;
    }
    const margin = isRelative(best, g) ? 0.13 : 0.08;
    if ((best.tonic !== g.tonic || best.minor !== g.minor) && best.r > gScore + margin && best.r > 0.56) {
      const sh = contextualSharps(best.tonic, best.minor, g.sharps);
      res = { tonic: best.tonic, minor: best.minor, sharps: sh, name: keyName(best.tonic, best.minor, sh) };
    }
  }
  m.localBins.set(bin, res);
  return res;
}

/** Smooth local scale / key readout at time `t` (3s bins confirmed across adjacent windows to prevent flicker). */
export function localKeyAt(m: KeyModel, t: number): { tonic: number; minor: boolean; sharps: number; name: string } {
  const g = { tonic: m.global.tonic, minor: m.global.minor, sharps: m.global.sharps, name: m.global.name };
  if (!m.allowLocal) return g;
  const bin = Math.max(0, Math.floor(Math.max(0, t) / 3));
  const c = rawLocalKey(m, bin);
  if (c.tonic === g.tonic && c.minor === g.minor) return g;
  const prev = rawLocalKey(m, Math.max(0, bin - 1));
  const next = rawLocalKey(m, bin + 1);
  if ((prev.tonic === c.tonic && prev.minor === c.minor) || (next.tonic === c.tonic && next.minor === c.minor)) {
    return c;
  }
  return g;
}
