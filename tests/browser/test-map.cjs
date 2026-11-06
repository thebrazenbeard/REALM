"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const map = require("../../docs/map.js");

function fixture() {
  return {
    space: { id: "region-1", name: "Green Shore", kind: "region" },
    width: 3, height: 2,
    tiles: [
      { x: 0, y: 0, glyph: "~", terrain: "water", passable: false },
      { x: 1, y: 0, glyph: "^", terrain: "forest", passable: true },
      { x: 2, y: 0, glyph: ".", terrain: "plain", passable: true },
    ],
    markers: [{ id: "s1", x: 2, y: 0, kind: "ship" }],
    legend: [{ glyph: "^", terrain: "trees" }],
    player: { x: 1, y: 0 },
  };
}

test("validates a sparse, authorized 2D map without filling unknown terrain", () => {
  const parsed = map.validateMap(fixture());
  assert.equal(parsed.rows[0][1].terrain, "forest");
  assert.equal(parsed.rows[1][1], null);
  assert.equal(parsed.markers.get("2,0"), "ship");
});

test("rejects excessive dimensions, tiles and marker data", () => {
  const a = fixture(); a.width = map.MAX_DIMENSION + 1;
  assert.throws(() => map.validateMap(a), /viewport limits/);
  const b = fixture(); b.tiles.push(...Array(10).fill({ x: 0, y: 0, glyph: ".", terrain: "plain" }));
  assert.throws(() => map.validateMap(b), /tile count/);
  const c = fixture(); c.markers = Array(65).fill({ x: 0, y: 0, kind: "ship" });
  assert.throws(() => map.validateMap(c), /marker count/);
});

test("rejects duplicate, invalid, or out-of-viewport coordinates", () => {
  const a = fixture(); a.tiles.push({ ...a.tiles[0] });
  assert.throws(() => map.validateMap(a), /duplicate tile/);
  const b = fixture(); b.player = { x: -1, y: 0 };
  assert.throws(() => map.validateMap(b), /player coordinates/);
  const c = fixture(); c.tiles[0].x = 3;
  assert.throws(() => map.validateMap(c), /tile coordinates/);
});

test("rejects escape characters and multi-character glyph injection", () => {
  const a = fixture(); a.tiles[0].glyph = "<script>";
  assert.throws(() => map.validateMap(a), /invalid glyph/);
  const b = fixture(); b.tiles[0].glyph = "\u001b";
  assert.throws(() => map.validateMap(b), /invalid glyph/);
});

function element() {
  return {
    children: [], attributes: {}, textContent: "", className: "",
    classList: { add() {}, remove() {} },
    append(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
    setAttribute(name, value) { this.attributes[name] = value; },
  };
}

test("renders player position, marker and terrain as inert text nodes", () => {
  const doc = { createElement: element, createDocumentFragment: element };
  const els = { grid: element(), space: element(), position: element(), status: element(), note: element(), legend: element() };
  const data = fixture();
  data.tiles[0].terrain = '<img src=x onerror=alert(1)>';
  map.renderMap(doc, els, data);
  const rows = els.grid.children[0].children;
  assert.equal(rows[0].children[0].textContent, "~");
  assert.equal(rows[0].children[1].textContent, "@");
  assert.equal(rows[0].children[2].textContent, "◆");
  assert.equal(rows[1].children[1].textContent, " ");
  assert.equal(rows[0].children[0].attributes["aria-label"], "0,0: <img src=x onerror=alert(1)>");
  assert.equal(els.status.textContent, "LIVE HOST MAP · 3×2");
  assert.equal(els.legend.textContent, "@ you  ·  ^ trees");
});

test("unavailable map explicitly clears the previous world snapshot", () => {
  const els = { grid: element(), space: element(), position: element(), status: element(), note: element(), legend: element() };
  els.grid.append(element());
  map.setUnavailable(els, "Host does not offer a map route.");
  assert.equal(els.grid.children.length, 0);
  assert.equal(els.note.textContent, "Host does not offer a map route.");
  assert.equal(els.status.textContent, "MAP UNAVAILABLE");
  assert.equal(els.grid.attributes["aria-label"], "No host map available");
});

test("unknown marker kinds cannot inherit Object prototype names", () => {
  const doc = { createElement: element, createDocumentFragment: element };
  const els = { grid: element(), space: element(), position: element(), status: element(), note: element(), legend: element() };
  const data = fixture(); data.markers[0].kind = "constructor";
  map.renderMap(doc, els, data);
  assert.equal(els.grid.children[0].children[0].children[2].textContent, "*");
});
