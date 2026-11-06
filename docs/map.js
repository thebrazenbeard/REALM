/* REALM map renderer. A host supplies authorized tiles; this file invents none. */
((scope) => {
  "use strict";

  const MAX_DIMENSION = 32;
  const MAX_MARKERS = 64;
  const TERRAIN_CLASSES = new Set([
    "water", "forest", "mountain", "plain", "road", "town", "building",
    "space", "rock", "desert", "unknown", "shore", "grass", "snow",
  ]);
  const MARKER_GLYPHS = Object.freeze({ ship: "◆", npc: "!", site: "+", portal: "◇" });

  function point(value, width, height, name) {
    if (!value || !Number.isSafeInteger(value.x) || !Number.isSafeInteger(value.y) ||
        value.x < 0 || value.y < 0 || value.x >= width || value.y >= height) {
      throw new Error(`Invalid ${name} coordinates in host map.`);
    }
    return { x: value.x, y: value.y };
  }

  function glyph(value) {
    if (typeof value !== "string" || Array.from(value).length !== 1 ||
        /[\u0000-\u001f\u007f\u2028\u2029]/u.test(value)) {
      throw new Error("Host map contains an invalid glyph.");
    }
    return value;
  }

  function safeText(value, max = 128) {
    return typeof value === "string" ? value.slice(0, max) : "";
  }

  function validateMap(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("Host map must be an object.");
    }
    const { width, height } = payload;
    if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) ||
        width < 1 || height < 1 || width > MAX_DIMENSION || height > MAX_DIMENSION) {
      throw new Error(`Host map exceeds ${MAX_DIMENSION}×${MAX_DIMENSION} viewport limits.`);
    }
    if (!payload.space || typeof payload.space.id !== "string" ||
        typeof payload.space.name !== "string" || typeof payload.space.kind !== "string") {
      throw new Error("Host map is missing its space identity.");
    }
    if (!Array.isArray(payload.tiles) || payload.tiles.length > width * height) {
      throw new Error("Host map has an invalid tile count.");
    }
    if (!Array.isArray(payload.markers) || payload.markers.length > MAX_MARKERS) {
      throw new Error("Host map has an invalid marker count.");
    }
    const rows = Array.from({ length: height }, () => Array(width).fill(null));
    for (const tile of payload.tiles) {
      const { x, y } = point(tile, width, height, "tile");
      if (rows[y][x]) throw new Error("Host map contains duplicate tile coordinates.");
      rows[y][x] = {
        glyph: glyph(tile.glyph),
        terrain: safeText(tile.terrain, 40),
        passable: tile.passable === true,
      };
    }
    const markers = new Map();
    for (const marker of payload.markers) {
      const { x, y } = point(marker, width, height, "marker");
      const key = `${x},${y}`;
      if (!markers.has(key)) markers.set(key, safeText(marker.kind, 40));
    }
    const player = point(payload.player, width, height, "player");
    const legend = Array.isArray(payload.legend) ? payload.legend.slice(0, 32).map((item) => ({
      glyph: glyph(item.glyph), terrain: safeText(item.terrain, 50),
    })) : [];
    return {
      space: { id: safeText(payload.space.id), name: safeText(payload.space.name), kind: safeText(payload.space.kind, 40) },
      width, height, rows, markers, player, legend,
    };
  }

  function renderMap(doc, elements, payload) {
    const map = validateMap(payload);
    const fragment = doc.createDocumentFragment();
    for (let y = 0; y < map.height; y++) {
      const row = doc.createElement("div");
      row.className = "map-row";
      row.setAttribute("role", "row");
      for (let x = 0; x < map.width; x++) {
        const tile = map.rows[y][x];
        const marker = map.markers.get(`${x},${y}`);
        const isPlayer = x === map.player.x && y === map.player.y;
        const cell = doc.createElement("span");
        const terrain = tile ? tile.terrain.toLowerCase() : "unknown";
        cell.className = "map-cell" + (TERRAIN_CLASSES.has(terrain) ? ` map-${terrain}` : "") +
          (marker ? " map-marker" : "") + (isPlayer ? " map-player" : "");
        cell.textContent = isPlayer ? "@" : (marker ? (MARKER_GLYPHS[marker] || "*") : (tile ? tile.glyph : " "));
        cell.setAttribute("role", "gridcell");
        cell.setAttribute("aria-label", `${x},${y}: ${isPlayer ? "your position" : marker || (tile ? terrain : "unexplored")}`);
        row.append(cell);
      }
      fragment.append(row);
    }
    elements.grid.replaceChildren(fragment);
    elements.grid.setAttribute("aria-label", `${map.space.name} map, ${map.width} columns by ${map.height} rows`);
    elements.space.textContent = `${map.space.name} / ${map.space.kind}`;
    elements.position.textContent = `@ ${map.player.x},${map.player.y}`;
    elements.status.textContent = `LIVE HOST MAP · ${map.width}×${map.height}`;
    elements.status.classList.add("is-live");
    elements.note.textContent = "The host controls visibility. Blank cells are unexplored or withheld.";
    const legend = map.legend.map((entry) => `${entry.glyph} ${entry.terrain}`).join("  ·  ");
    elements.legend.textContent = legend ? `@ you  ·  ${legend}` : "@ you  ·  * other marker";
    return map;
  }

  function setUnavailable(elements, message = "No host map available.") {
    elements.grid.replaceChildren();
    elements.space.textContent = "Unknown location";
    elements.position.textContent = "—";
    elements.status.textContent = "MAP UNAVAILABLE";
    elements.status.classList.remove("is-live");
    elements.note.textContent = message;
    elements.legend.textContent = "";
  }

  const api = Object.freeze({ MAX_DIMENSION, validateMap, renderMap, setUnavailable });
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else scope.RealmMap = api;
})(typeof window !== "undefined" ? window : globalThis);
