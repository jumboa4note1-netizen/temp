/* ------------------------------------------------------------------ *
 *  Fahh MIDI · performance tempo & beat estimation
 *
 *  Score MIDIs already carry calibrated tempo maps and barlines.
 *  Performance / audio-transcribed MIDIs (such as pianocore) store a
 *  dummy 120 BPM 4/4 header and unquantized timestamps. This module
 *  detects those files and recovers an accurate BPM curve plus tracked
 *  beat & measure grid from the note onsets.
 * ------------------------------------------------------------------ */

import type { TempoCue } from "./metadata";

export interface TempoNote {
  start: number;
  end: number;
  tick: number;
  midi: number;
  vel: number;
}

export interface TempoPointLike {
  tick: number;
  time: number;
  bpm: number;
  usPerBeat: number;
}

export interface MeasureLike {
  number: number;
  time: number;
  tick: number;
  beats: number[]; // beat times inside the measure excluding downbeat
  numerator: number;
  denominator: number;
}

export interface TimeSigLike {
  tick: number;
  numerator: number;
  denominator: number;
}

export interface BeatInfo {
  measure: number;
  beat: number; // 1..beatCount (0 during lead-in)
  beatCount: number;
  phase: number; // 0..1 within current beat
  beatBpm: number; // instantaneous BPM of current beat when sounding
}

const CUE_PRIOR: Record<Exclude<TempoCue, null>, { mean: number; sigma: number }> = {
  largo: { mean: 50, sigma: 0.25 },
  lento: { mean: 56, sigma: 0.24 },
  adagio: { mean: 64, sigma: 0.24 },
  andante: { mean: 80, sigma: 0.24 },
  moderato: { mean: 96, sigma: 0.24 },
  allegro: { mean: 118, sigma: 0.24 },
  presto: { mean: 136, sigma: 0.26 },
  waltz: { mean: 116, sigma: 0.26 },
};

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));

export function computeGridShare(notes: readonly TempoNote[], ppq: number): number {
  if (!notes.length || ppq <= 0) return 1;
  const step = Math.max(1, ppq / 12);
  const tol = Math.max(2, step * 0.12);
  const sample = Math.min(notes.length, 400);
  let onGrid = 0;
  for (let i = 0; i < sample; i++) {
    const q = Math.round(notes[i].tick / step) * step;
    if (Math.abs(notes[i].tick - q) <= tol) onGrid++;
  }
  return onGrid / sample;
}

export function isUnquantizedPerformance(
  notes: readonly TempoNote[],
  rawTempos: readonly TempoPointLike[],
  timeSigs: readonly TimeSigLike[],
  ppq: number,
): boolean {
  if (notes.length < 16) return false;
  if (rawTempos.length > 1) return false;
  if (timeSigs.length > 1) return false;
  const bpm0 = rawTempos[0]?.bpm ?? 120;
  if (Math.abs(bpm0 - 120) > 0.15) return false;
  return computeGridShare(notes, ppq) < 0.45;
}

export function estimatePerformanceTempoAndBeats(
  notes: readonly TempoNote[],
  ppq: number,
  tempoCue: TempoCue,
): { tempos: TempoPointLike[]; measures: MeasureLike[]; numerator: number; denominator: number } {
  const duration = notes.reduce((mx, n) => Math.max(mx, n.end), 1);
  // 100 Hz onset strength envelope
  const Fs = 100;
  const N = Math.max(200, Math.ceil((duration + 2) * Fs));
  const env = new Float64Array(N);

  // Cluster macro-onsets within 42ms so simultaneous chords count as a strong beat impulse
  const clusters: { t: number; weight: number }[] = [];
  for (const n of notes) {
    const w0 = (0.45 + 0.55 * (n.vel / 127)) * (n.midi < 54 ? 1.35 : 1.0);
    const last = clusters[clusters.length - 1];
    if (last && n.start - last.t <= 0.042) {
      last.weight += w0 * 0.35;
    } else {
      clusters.push({ t: n.start, weight: w0 });
    }
  }
  // Slightly attenuate ultra-fast ornament clusters while boosting accented notes after rests
  for (let i = 0; i < clusters.length; i++) {
    const gap = i > 0 ? clusters[i].t - clusters[i - 1].t : 0.6;
    const gapBoost = gap > 0.35 ? 1.3 : gap < 0.09 ? 0.55 : 1.0;
    const idx = clamp(Math.round(clusters[i].t * Fs), 0, N - 1);
    const w = clusters[i].weight * gapBoost;
    // Gaussian spread +-30ms (3 bins)
    for (let d = -3; d <= 3; d++) {
      const j = idx + d;
      if (j >= 0 && j < N) env[j] += w * Math.exp(-(d * d) / 3.2);
    }
  }

  // Sliding window tempo curve (10s window, 2.5s hop)
  const BPM_MIN = 46;
  const BPM_MAX = 180;
  const BINS = BPM_MAX - BPM_MIN + 1;
  const priorInfo = tempoCue ? CUE_PRIOR[tempoCue] : { mean: 92, sigma: 0.42 };

  const winSec = 10;
  const hopSec = 2.5;
  const winStarts: number[] = [];
  const winScores: Float64Array[] = [];
  for (let t0 = 0; t0 < Math.max(0.5, duration - 3); t0 += hopSec) {
    const a = Math.max(0, Math.floor(t0 * Fs));
    const b = Math.min(N, Math.floor((t0 + winSec) * Fs));
    if (b - a < 150) break;
    let energy = 0;
    for (let i = a; i < b; i++) energy += env[i];
    if (energy <= 1e-6) continue;

    const acfMaxLag = Math.min(b - a - 1, Math.round(Fs * 2.8));
    const acf = new Float64Array(acfMaxLag + 1);
    for (let lag = 20; lag <= acfMaxLag; lag++) {
      let s = 0;
      const lim = b - lag;
      for (let i = a; i < lim; i++) s += env[i] * env[i + lag];
      acf[lag] = s;
    }
    let maxAcf = 1e-9;
    for (let lag = 25; lag <= acfMaxLag; lag++) if (acf[lag] > maxAcf) maxAcf = acf[lag];

    const sc = new Float64Array(BINS);
    for (let bi = 0; bi < BINS; bi++) {
      const bpm = BPM_MIN + bi;
      const l1 = Math.round((60 / bpm) * Fs);
      const l2 = Math.min(acfMaxLag, l1 * 2);
      const lh = Math.max(20, Math.round(l1 * 0.5));
      const v1 = l1 <= acfMaxLag ? acf[l1] / maxAcf : 0;
      const v2 = l2 <= acfMaxLag ? acf[l2] / maxAcf : 0;
      const vh = lh <= acfMaxLag ? acf[lh] / maxAcf : 0;
      const comb = 0.6 * v1 + 0.28 * v2 + 0.12 * vh;
      const z = Math.log(bpm / priorInfo.mean) / priorInfo.sigma;
      const prior = Math.exp(-0.5 * z * z);
      sc[bi] = comb * (0.72 + 0.28 * prior);
    }
    winStarts.push(t0);
    winScores.push(sc);
  }

  // Fallback if piece is extremely short (<2s)
  if (!winScores.length) {
    const bpm = priorInfo.mean;
    const usPerBeat = Math.round(6e7 / bpm);
    const beatDur = 60 / bpm;
    const measures: MeasureLike[] = [];
    let num = 1;
    for (let t = 0; t <= duration; t += beatDur * 4, num++) {
      measures.push({ number: num, time: t, tick: Math.round((t * bpm * ppq) / 60), beats: [t + beatDur, t + 2 * beatDur, t + 3 * beatDur], numerator: 4, denominator: 4 });
    }
    return { tempos: [{ tick: 0, time: 0, bpm, usPerBeat }], measures, numerator: 4, denominator: 4 };
  }

  // Viterbi smooth BPM track across windows (strong penalty for sudden octave jumps)
  const W = winScores.length;
  const dp: Float64Array[] = Array.from({ length: W }, () => new Float64Array(BINS));
  const back: Int16Array[] = Array.from({ length: W }, () => new Int16Array(BINS));
  for (let b = 0; b < BINS; b++) dp[0][b] = winScores[0][b];
  for (let w = 1; w < W; w++) {
    for (let b = 0; b < BINS; b++) {
      let bestVal = -Infinity;
      let bestPrev = 0;
      for (let pb = 0; pb < BINS; pb++) {
        const diff = Math.abs(b - pb);
        const pen = diff <= 3 ? diff * 0.004 : diff <= 12 ? 0.012 + (diff - 3) * 0.015 : 0.25 + diff * 0.01;
        const val = dp[w - 1][pb] - pen;
        if (val > bestVal) {
          bestVal = val;
          bestPrev = pb;
        }
      }
      dp[w][b] = bestVal + winScores[w][b];
      back[w][b] = bestPrev;
    }
  }
  let bestB = 0;
  for (let b = 1; b < BINS; b++) if (dp[W - 1][b] > dp[W - 1][bestB]) bestB = b;
  const winBpms = new Array<number>(W);
  for (let w = W - 1; w >= 0; w--) {
    winBpms[w] = BPM_MIN + bestB;
    bestB = back[w][bestB];
  }

  const targetBpmAt = (t: number) => {
    if (winStarts.length === 1) return winBpms[0];
    const centerT = Math.max(0, t - winSec * 0.35);
    const pos = centerT / hopSec;
    const i0 = clamp(Math.floor(pos), 0, W - 1);
    const i1 = clamp(i0 + 1, 0, W - 1);
    const f = pos - i0;
    return winBpms[i0] * (1 - f) + winBpms[i1] * f;
  };

  // Phase alignment + predictive beat tracking onto actual note onset peaks
  const firstNoteT = clusters[0]?.t ?? 0;
  const p0 = 60 / winBpms[0];
  let bestPhase = firstNoteT;
  let bestPhaseScore = -Infinity;
  const scanStart = Math.max(0, firstNoteT - p0 * 0.2);
  const scanEnd = Math.min(duration, firstNoteT + p0 * 1.1);
  for (let ph = scanStart; ph <= scanEnd; ph += 0.02) {
    let sc = 0;
    for (let k = 0; k < 8; k++) {
      const idx = Math.round((ph + k * p0) * Fs);
      if (idx < N) sc += env[idx];
    }
    if (sc > bestPhaseScore) {
      bestPhaseScore = sc;
      bestPhase = ph;
    }
  }

  const beatTimes: number[] = [bestPhase];
  while (beatTimes[beatTimes.length - 1] < duration + 0.5) {
    const cur = beatTimes[beatTimes.length - 1];
    const period = 60 / targetBpmAt(cur);
    const tLow = cur + period * 0.82;
    const tHigh = cur + period * 1.18;
    const iLow = clamp(Math.floor(tLow * Fs), 0, N - 1);
    const iHigh = clamp(Math.ceil(tHigh * Fs), iLow, N - 1);
    let bestT = cur + period;
    let bestBeatScore = -Infinity;
    let maxE = 0.01;
    for (let i = iLow; i <= iHigh; i++) if (env[i] > maxE) maxE = env[i];
    for (let i = iLow; i <= iHigh; i++) {
      const candT = i / Fs;
      const err = (candT - cur - period) / period;
      const s = (env[i] / maxE) * 0.55 - 4.2 * err * err;
      if (s > bestBeatScore) {
        bestBeatScore = s;
        bestT = candT;
      }
    }
    if (bestT <= cur + 0.15) bestT = cur + period;
    beatTimes.push(bestT);
    if (beatTimes.length > 6000) break;
  }

  // Decide 3/4 vs 4/4 meter cleanly
  let score3 = 0;
  let score4 = 0;
  for (let i = 0; i + 4 < Math.min(beatTimes.length, 64); i++) {
    const e0 = env[clamp(Math.round(beatTimes[i] * Fs), 0, N - 1)];
    if (i % 3 === 0) score3 += e0;
    if (i % 4 === 0) score4 += e0;
  }
  const n3 = Math.max(1, Math.floor(Math.min(beatTimes.length, 64) / 3));
  const n4 = Math.max(1, Math.floor(Math.min(beatTimes.length, 64) / 4));
  const isThree = tempoCue === "waltz" || (score3 / n3 > (score4 / n4) * 1.16);
  const numerator = isThree ? 3 : 4;
  const denominator = 4;

  // Build smooth TempoPoint[] list (emitting a point at t=0 and whenever BPM shifts >= 2.5 BPM)
  const tempos: TempoPointLike[] = [];
  let accumTicks = 0;
  let prevBeatT = 0;
  const initBpm = winBpms[0];
  tempos.push({ tick: 0, time: 0, bpm: initBpm, usPerBeat: Math.round(6e7 / initBpm) });
  for (let i = 1; i < beatTimes.length; i++) {
    const dt = Math.max(0.25, beatTimes[i] - beatTimes[i - 1]);
    const instBpm = clamp(60 / dt, BPM_MIN, BPM_MAX);
    const lastTp = tempos[tempos.length - 1];
    accumTicks += ((beatTimes[i - 1] - prevBeatT) * lastTp.bpm * ppq) / 60;
    prevBeatT = beatTimes[i - 1];
    if (beatTimes[i - 1] - lastTp.time >= 2.5 && Math.abs(instBpm - lastTp.bpm) >= 2.5) {
      const smoothed = Math.round(lastTp.bpm * 0.35 + instBpm * 0.65);
      tempos.push({
        tick: Math.round(accumTicks),
        time: beatTimes[i - 1],
        bpm: smoothed,
        usPerBeat: Math.round(6e7 / smoothed),
      });
    }
  }

  // Group tracked beats into measures
  const measures: MeasureLike[] = [];
  for (let i = 0, num = 1; i < beatTimes.length && beatTimes[i] <= duration + 0.1; i += numerator, num++) {
    const beats: number[] = [];
    for (let b = 1; b < numerator && i + b < beatTimes.length; b++) beats.push(beatTimes[i + b]);
    const mTime = beatTimes[i];
    const tp = tempos.reduce((acc, p) => (p.time <= mTime ? p : acc), tempos[0]);
    const mTick = Math.round(tp.tick + ((mTime - tp.time) * tp.bpm * ppq) / 60);
    measures.push({ number: num, time: mTime, tick: mTick, beats, numerator, denominator });
  }

  return { tempos, measures, numerator, denominator };
}

export function beatInfoAt(measures: readonly MeasureLike[], tempos: readonly TempoPointLike[], t: number, fallbackDuration = 60): BeatInfo {
  const activeTp = (() => {
    let lo = 0;
    let hi = Math.max(0, tempos.length - 1);
    while (lo < hi) {
      const m = (lo + hi + 1) >> 1;
      if (tempos[m].time <= Math.max(0, t)) lo = m;
      else hi = m - 1;
    }
    return tempos[lo] ?? { bpm: 120, time: 0, tick: 0, usPerBeat: 500000 };
  })();

  if (!measures.length || t < measures[0].time - 1e-6) {
    return { measure: 0, beat: 0, beatCount: measures[0]?.numerator ?? 4, phase: 0, beatBpm: activeTp.bpm };
  }

  let lo = 0;
  let hi = measures.length - 1;
  while (lo < hi) {
    const m = (lo + hi + 1) >> 1;
    if (measures[m].time <= t + 1e-6) lo = m;
    else hi = m - 1;
  }
  const m = measures[lo];
  const nextMeasure = measures[lo + 1];
  const allBeats = [m.time, ...m.beats];
  const beatCount = Math.max(1, allBeats.length);
  const measureEnd = nextMeasure ? nextMeasure.time : Math.max(m.time + (60 / Math.max(30, activeTp.bpm)) * beatCount, fallbackDuration);

  let bIdx = 0;
  for (let i = 1; i < allBeats.length; i++) {
    if (allBeats[i] <= t + 1e-6) bIdx = i;
    else break;
  }
  const bStart = allBeats[bIdx];
  const bEnd = bIdx + 1 < allBeats.length ? allBeats[bIdx + 1] : measureEnd;
  const span = Math.max(0.15, bEnd - bStart);
  const phase = clamp((t - bStart) / span, 0, 1);
  const beatBpm = clamp(60 / span, 36, 220);

  return {
    measure: m.number,
    beat: bIdx + 1,
    beatCount,
    phase,
    beatBpm,
  };
}
