import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { buildKeyModel, keySharps, localKeyAt } from "./harmony";
import { parseMidi } from "./midi";
import { beatInfoAt, computeGridShare, estimatePerformanceTempoAndBeats, isUnquantizedPerformance } from "./tempo";

function makeQuantizedCMinorNotes() {
  // 8 measures in 4/4 at 100 BPM (beat = 0.6s) anchored clearly in C minor (C, D, Eb, F, G, Ab, B/Bb)
  const ppq = 480;
  const chordSeq = [
    [48, 60, 63, 67], // Cm
    [53, 65, 68, 72], // Fm
    [55, 67, 71, 74], // G major
    [48, 60, 63, 67], // Cm
  ];
  const notes = [];
  for (let bar = 0; bar < 8; bar++) {
    const ch = chordSeq[bar % chordSeq.length];
    for (let b = 0; b < 4; b++) {
      const start = (bar * 4 + b) * 0.6;
      const tick = (bar * 4 + b) * ppq;
      for (const midi of ch) {
        notes.push({ midi, start, end: start + 0.5, tick, vel: b === 0 ? 102 : 76 });
      }
    }
  }
  return { notes, ppq };
}

test("key model uses canonical piano enharmonics and estimates C minor accurately", () => {
  const { notes } = makeQuantizedCMinorNotes();
  const model = buildKeyModel(notes, null, true, null);
  assert.equal(model.global.name, "C minor");
  assert.equal(model.global.tonic, 0);
  assert.equal(model.global.minor, true);
  assert.equal(keySharps(3, true), -6); // E♭ minor instead of D♯ minor
  assert.equal(keySharps(1, false), -5); // D♭ major
  assert.equal(localKeyAt(model, 4.0).name, "C minor");
});

test("performance tempo detector leaves quantized scores untouched and recovers unquantized dummy-120 tempo & beats", () => {
  const { notes, ppq } = makeQuantizedCMinorNotes();
  assert.equal(computeGridShare(notes, ppq), 1);
  assert.equal(
    isUnquantizedPerformance(notes, [{ tick: 0, time: 0, bpm: 120, usPerBeat: 500000 }], [{ tick: 0, numerator: 4, denominator: 4 }], ppq),
    false,
  );

  // Create unquantized performance MIDI at 92 BPM (beat = 0.652s) with slight human micro-timing jitter
  const beatDur = 60 / 92;
  const perfNotes = [];
  for (let b = 0; b < 48; b++) {
    const jitter = ((b % 5) - 2) * 0.007;
    const t = 0.25 + b * beatDur + jitter;
    const tick = Math.round(t * 960) + 17;
    perfNotes.push({ midi: b % 4 === 0 ? 48 : 60, start: t, end: t + 0.35, tick, vel: b % 4 === 0 ? 106 : 74 });
    perfNotes.push({ midi: 67, start: t + 0.005, end: t + 0.35, tick: tick + 5, vel: 70 });
  }
  assert.equal(
    isUnquantizedPerformance(perfNotes, [{ tick: 0, time: 0, bpm: 120, usPerBeat: 500000 }], [{ tick: 0, numerator: 4, denominator: 4 }], 480),
    true,
  );

  const est = estimatePerformanceTempoAndBeats(perfNotes, 480, null);
  assert.ok(est.tempos.length >= 1);
  assert.ok(est.tempos[0].bpm >= 84 && est.tempos[0].bpm <= 100, `expected ~92 BPM, got ${est.tempos[0].bpm}`);
  assert.ok(est.measures.length >= 6);

  const bInfo = beatInfoAt(est.measures, est.tempos, est.measures[1].time + 0.05, 40);
  assert.equal(bInfo.measure, est.measures[1].number);
  assert.equal(bInfo.beat, 1);
  assert.equal(bInfo.beatCount, est.numerator);
});

test("real HF sample MIDIs parse cleanly with realistic BPM and expected key signatures", () => {
  const sampleDir = "/tmp/hf_samples";
  if (!fs.existsSync(sampleDir)) return;
  const cases: [string, string][] = [
    ["Alkan,_Charles-Valentin - 12_Etudes_in_All_the_minor_Keys,_Op.39 - 7._Finale._Presto_(E_flat_minor).mid", "E♭ minor"],
    ["Chopin,_Frédéric - Waltzes,_Op.64 - Waltz_No.7_in_C_sharp_minor,_Tempo_giusto.mid", "C♯ minor"],
    ["Bach,_Johann_Sebastian - Das_Wohltemperierte_Klavier,_Teil_I,_BWV_846-869 - 15._Fuga.mid", "G major"],
  ];
  for (const [fn, expectedKey] of cases) {
    const p = path.join(sampleDir, fn);
    if (!fs.existsSync(p)) continue;
    const song = parseMidi(new Uint8Array(fs.readFileSync(p)), { fileName: fn });
    assert.equal(song.key.name, expectedKey, `expected ${expectedKey} for ${fn}, got ${song.key.name}`);
    assert.ok(song.tempos[0].bpm > 45 && song.tempos[0].bpm < 185);
    assert.ok(song.measures.length > 4);
  }
});
