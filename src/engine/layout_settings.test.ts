import test from "node:test";
import assert from "node:assert/strict";
import { DEFAULTS, FIELD_BY_PATH, SCHEMA, resolvePath } from "./settings";

test("minimap and chord settings are completely removed from schema and defaults", () => {
  assert.equal(DEFAULTS.video.width, 1920);
  assert.equal(DEFAULTS.video.height, 1200);
  assert.equal(Math.round((DEFAULTS.video.width / DEFAULTS.video.height) * 100), 160);

  for (const f of SCHEMA) {
    assert.ok(!/chord/i.test(f.path), `unexpected chord path in schema: ${f.path}`);
    assert.ok(!/minimap/i.test(f.path), `unexpected minimap path in schema: ${f.path}`);
  }
  assert.equal(FIELD_BY_PATH.get("render.showKeyMinimap"), undefined);
  assert.equal(FIELD_BY_PATH.get("render.showChordBanner"), undefined);
  assert.equal(FIELD_BY_PATH.get("render.showChordBrackets"), undefined);
  assert.equal(resolvePath("width"), "video.width");
  assert.equal(resolvePath("height"), "video.height");
});

test("subtle vintage color grade is baked into render defaults", () => {
  const r = DEFAULTS.render;
  assert.ok(r.post.sepia > 0 && r.post.sepia <= 0.15);
  assert.ok(r.post.vignette > 0.15 && r.post.vignette <= 0.35);
  assert.ok(r.post.contrast >= 1.0 && r.post.contrast <= 1.15);
  assert.ok(r.post.saturation >= 0.85 && r.post.saturation <= 1.0);
  assert.equal(r.colors.hudPanel, "#1c150f");
  assert.equal(r.colors.rightHand, "#e8b34c");
  assert.equal(r.colors.leftHand, "#52b89c");
});
