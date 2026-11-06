"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const RealmMap = require("../../docs/map.js");

const clientSource = process.env.REALM_CLIENT_SOURCE_B64
  ? Buffer.from(process.env.REALM_CLIENT_SOURCE_B64, "base64").toString("utf8")
  : fs.readFileSync(path.join(__dirname, "../../docs/app.js"), "utf8");

class Element {
  constructor() {
    this.value = ""; this.textContent = ""; this.children = []; this.disabled = false;
    this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 400;
    this.events = {}; this.attributes = {};
    this.dataset = {};
    const classes = new Set();
    this.classList = {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
      contains: (name) => classes.has(name),
    };
  }
  addEventListener(type, fn) { this.events[type] = fn; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  setAttribute(name, value) { this.attributes[name] = value; }
  get firstElementChild() { return this.children[0] || null; }
  remove() {}
  reportValidity() { return true; }
  focus() {}
}

function worldMap() {
  return {
    space: { id: "site", name: "Test Region", kind: "region" },
    width: 2, height: 2,
    tiles: [{ x: 0, y: 0, glyph: "^", terrain: "forest", passable: true }],
    markers: [], legend: [{ glyph: "^", terrain: "trees" }], player: { x: 0, y: 0 },
  };
}

function instantiate({ missingMap = false, mapOnMove = false, rejectUnauthorized = false } = {}) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const requests = [];
  let mapAvailable = !missingMap;
  const document = {
    getElementById: get,
    querySelectorAll: () => [],
    createElement: () => new Element(),
    createDocumentFragment: () => new Element(),
  };
  const responses = {
    "/api/v1/health": { ok: true, world_id: "test-world" },
    "/api/v1/join": { text: "Welcome", cursor: 0 },
    "/api/v1/room": { text: "You are in a field by water." },
    "/api/v1/map": worldMap(),
    "/api/v1/feed": { cursor: 0, events: [] },
    "/api/v1/command": { text: "You move north", cursor: 0 },
  };
  const fetch = async (url, options) => {
    requests.push({ url, options });
    const route = new URL(url).pathname;
    if (rejectUnauthorized && route === "/api/v1/join") {
      return { ok: false, status: 401, json: async () => ({ error: "Player token mismatch" }) };
    }
    if (route === "/api/v1/map" && !mapAvailable) {
      return { ok: false, status: 404, json: async () => ({ error: "No map is configured for this location" }) };
    }
    if (route === "/api/v1/command" && mapOnMove) mapAvailable = true;
    assert.ok(route in responses, "unexpected route " + route);
    return { ok: true, status: 200, json: async () => responses[route] };
  };
  const window = { location: { protocol: "http:" }, RealmMap };
  vm.runInNewContext(clientSource, {
    document, window, URL, fetch, console,
    setInterval: () => 1, clearInterval: () => {},
    crypto: require("node:crypto").webcrypto,
  }, { filename: "docs/app.js" });
  const event = { preventDefault() {} };
  return { get, requests, connect: () => get("connection-form").events.submit(event),
    disconnect: () => get("disconnect-button").events.click(),
    send: () => get("command-form").events.submit(event) };
}

test("successful join requests authorized map, clears DOM token, and renders host tiles", async () => {
  const client = instantiate();
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  assert.equal(client.get("world-name").textContent, "test-world");
  assert.equal(client.get("access-token").value, "");
  assert.equal(client.get("map-status").textContent, "LIVE HOST MAP · 2×2");
  assert.ok(client.requests.some(({ url }) => url.includes("/api/v1/map?player=Explorer")));
  assert.ok(client.requests.every(({ options }) => options.credentials === "omit"));
  client.disconnect();
  assert.equal(client.get("map-status").textContent, "MAP UNAVAILABLE");
  assert.equal(client.get("world-name").textContent, "Unconnected");
});

test("a host without map support stays connected to its usable MUD", async () => {
  const client = instantiate({ missingMap: true });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
  assert.equal(client.get("map-status").textContent, "MAP UNAVAILABLE");
  assert.match(client.get("map-note").textContent, /No map is available here/);
  client.get("command-input").value = "go north";
  await client.send();
  assert.equal(client.get("command-input").value, "");
  assert.equal(client.requests.filter(({ url }) => url.includes("/api/v1/map")).length, 2);
});

test("moving from unmapped to mapped territory refreshes map after initial 404", async () => {
  const client = instantiate({ missingMap: true, mapOnMove: true });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "player-token";
  await client.connect();
  assert.equal(client.get("map-status").textContent, "MAP UNAVAILABLE");
  client.get("command-input").value = "go north";
  await client.send();
  assert.equal(client.get("map-status").textContent, "LIVE HOST MAP · 2×2");
});

test("a mismatched player token never reaches a joined session", async () => {
  const client = instantiate({ rejectUnauthorized: true });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "wrong-player-token";
  await client.connect();
  assert.equal(client.get("world-name").textContent, "Unconnected");
  assert.match(client.get("connection-note").textContent, /Player token mismatch/);
  assert.ok(client.requests.some(({ options }) => options.headers.Authorization === "Bearer wrong-player-token"));
});

test("refuses cleartext remote worlds before sending any bearer token", async () => {
  const client = instantiate();
  client.get("host-url").value = "http://remote.example:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  assert.equal(client.requests.length, 0);
  assert.match(client.get("connection-note").textContent, /HTTP is permitted only for loopback/);
});
