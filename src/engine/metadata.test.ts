import test from "node:test";
import assert from "node:assert/strict";
import { detectTempoCue, extractLeafKeyHint, parseSongFilename } from "./metadata";

test("parses 3-part HF catalog filenames with Last, First and movement title", () => {
  const m = parseSongFilename(
    "Alkan,_Charles-Valentin - 12_Etudes_in_All_the_minor_Keys,_Op.39 - 7._Finale._Presto_(E_flat_minor).mid",
  );
  assert.equal(m.artist, "Charles-Valentin Alkan");
  assert.equal(m.piece, "12 Etudes in All the minor Keys, Op. 39");
  assert.equal(m.displayTitle, "7. Finale. Presto (E♭ minor)");
  assert.equal(m.displaySub, "Charles-Valentin Alkan · 12 Etudes in All the minor Keys, Op. 39");
  assert.equal(m.tempoCue, "presto");
  assert.deepEqual(m.filenameKeyHint, {
    tonic: 3,
    minor: true,
    sharps: -3,
    preferredName: "E♭ minor",
  });
});

test("uses only movement leaf for multi-movement sonatas so parent key does not mislead", () => {
  const m = parseSongFilename(
    "Beethoven,_Ludwig_van - Piano_Sonata_No.31_in_A_flat_major,_Op.110 - 2._Allegro_molto.mid",
  );
  assert.equal(m.artist, "Ludwig van Beethoven");
  assert.equal(m.displayTitle, "2. Allegro molto");
  assert.equal(m.displaySub, "Ludwig van Beethoven · Piano Sonata No. 31 in A♭ major, Op. 110");
  assert.equal(m.filenameKeyHint, null);
  assert.equal(m.tempoCue, "presto");
});

test("normalizes C sharp minor, 2-part filenames, and opus spacing cleanly", () => {
  const m = parseSongFilename("Chopin,_Frédéric - Waltzes,_Op.64 - Waltz_No.7_in_C_sharp_minor,_Tempo_giusto.mid");
  assert.equal(m.artist, "Frédéric Chopin");
  assert.equal(m.displayTitle, "Waltz No. 7 in C♯ minor, Tempo giusto");
  assert.equal(m.displaySub, "Frédéric Chopin · Waltzes, Op. 64");
  assert.equal(m.filenameKeyHint?.preferredName, "C♯ minor");

  const two = parseSongFilename("Albéniz,_Isaac - España,_Op.165 - 2._Tango._Andantino.mid");
  assert.equal(two.artist, "Isaac Albéniz");
  assert.equal(two.tempoCue, "andante");

  const twoOnly = parseSongFilename("Bartók,_Béla - Suite_Op.14.mid");
  assert.equal(twoOnly.artist, "Béla Bartók");
  assert.equal(twoOnly.displayTitle, "Suite Op. 14");
  assert.equal(twoOnly.displaySub, "Béla Bartók");
});

test("detectTempoCue and extractLeafKeyHint handle edge cases gracefully", () => {
  assert.equal(detectTempoCue("Menuet en Sol"), "waltz");
  assert.equal(detectTempoCue("Plain Title"), null);
  assert.equal(extractLeafKeyHint("Preludio"), null);
  assert.equal(extractLeafKeyHint("Fugue in F sharp minor")?.tonic, 6);
});
