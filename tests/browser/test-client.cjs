"use strict";

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
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

function instantiate({ missingMap = false, mapOnMove = false, rejectUnauthorized = false, fetchImpl = null, mapGate = null, roomGate = null, joinGate = null, nowMs = null } = {}) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const requests = [];
  const timers = [];
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
  const mockFetch = async (url, options) => {
    requests.push({ url, options });
    const route = new URL(url).pathname;
    if (route === "/api/v1/map" && mapGate) await mapGate();
    if (route === "/api/v1/room" && roomGate) await roomGate();
    if (route === "/api/v1/join" && joinGate) await joinGate();
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
  const fetch = fetchImpl || mockFetch;
  const window = { location: { protocol: "http:" }, RealmMap };
  vm.runInNewContext(clientSource, {
    document, window, URL, fetch, console,
    setInterval: (fn, delay) => { timers.push({ fn, delay }); return timers.length; },
    clearInterval: () => {},
    crypto: require("node:crypto").webcrypto, AbortController,
    Date: nowMs ? class extends Date { static now() { return nowMs.value; } } : Date,
  }, { filename: "docs/app.js" });
  const event = { preventDefault() {} };
  return { get, requests, timers, connect: () => get("connection-form").events.submit(event),
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

test("a slow periodic map fetch cannot overwrite a post-command position", async () => {
  let resolveOldMap, signalOldMap;
  const oldMapEntered = new Promise((resolve) => { signalOldMap = resolve; });
  const releaseOldMap = new Promise((resolve) => { resolveOldMap = resolve; });
  let playerX = 0;
  let mapCalls = 0;
  const clock = { value: 1000 };
  const client = instantiate({ nowMs: clock, fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/map") {
      const snapshot = playerX;
      mapCalls++;
      if (mapCalls === 2) { signalOldMap(); await releaseOldMap; }
      return { ok: true, status: 200, json: async () => ({
        ...worldMap(), player: { x: snapshot, y: 0 },
      }) };
    }
    if (route === "/api/v1/command") playerX = 1;
    const payload = route === "/api/v1/health" ? { ok: true, world_id: "race-world" }
      : route === "/api/v1/join" ? { text: "Welcome", cursor: 0 }
        : route === "/api/v1/room" ? { text: "Deck" }
          : route === "/api/v1/feed" ? { cursor: 0, events: [] }
            : { text: "Moved east" };
    return { ok: true, status: 200, json: async () => payload };
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  assert.equal(client.get("map-position").textContent, "@ 0,0");
  clock.value += 15000;
  const stalePoll = client.timers[0].fn();
  await oldMapEntered;
  client.get("command-input").value = "go east";
  await client.send();
  assert.equal(playerX, 1, "host movement must have completed");
  resolveOldMap();
  await stalePoll;
  assert.equal(client.get("map-position").textContent, "@ 1,0",
    "an older map response must not hide a movement whose command already committed");
  assert.equal(mapCalls, 3, "an in-flight old map requires one coalesced post-command refresh");
});

test("a slow initial room response cannot overwrite a room reached by a command", async () => {
  let enterOldRoom, releaseOldRoom;
  const oldRoomEntered = new Promise((resolve) => { enterOldRoom = resolve; });
  const pendingOldRoom = new Promise((resolve) => { releaseOldRoom = resolve; });
  let room = "Old Deck", roomCalls = 0;
  const client = instantiate({ fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/room") {
      const snapshot = room;
      if (++roomCalls === 1) { enterOldRoom(); await pendingOldRoom; }
      return { ok: true, status: 200, json: async () => ({ text: snapshot }) };
    }
    if (route === "/api/v1/command") room = "New Deck";
    const payload = route === "/api/v1/health" ? { ok: true, world_id: "room-race" }
      : route === "/api/v1/join" ? { text: "Welcome", cursor: 0 }
        : route === "/api/v1/map" ? worldMap()
          : route === "/api/v1/feed" ? { cursor: 0, events: [] }
            : { text: "Moved east" };
    return { ok: true, status: 200, json: async () => payload };
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  const entering = client.connect();
  await oldRoomEntered;
  client.get("command-input").value = "go east";
  await client.send();
  assert.equal(room, "New Deck");
  assert.equal(client.get("room-description").textContent, "New Deck");
  releaseOldRoom();
  await entering;
  assert.equal(roomCalls, 2);
  assert.equal(client.get("room-description").textContent, "New Deck",
    "a stale initial room snapshot must not overwrite the later authorized command result");
});

test("disconnect during initial map fetch does not leave an orphan polling timer", async () => {
  let releaseMap;
  let reachedMap;
  const enteredMap = new Promise((resolve) => { reachedMap = resolve; });
  const pendingMap = new Promise((resolve) => { releaseMap = resolve; });
  const client = instantiate({ mapGate: () => { reachedMap(); return pendingMap; } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  const connecting = client.connect();
  await enteredMap;
  assert.equal(client.get("disconnect-button").disabled, false);
  client.disconnect();
  const mapRequest = client.requests.find(({ url }) => url.includes("/api/v1/map"));
  assert.equal(mapRequest.options.signal?.aborted, true, "disconnect should abort in-flight host requests");
  releaseMap();
  await connecting;
  assert.equal(client.timers.length, 0, "disconnected session must not install a recurring poller");
  assert.equal(client.get("world-name").textContent, "Unconnected");
});

test("late room failure from an old connection cannot corrupt a rejoined transcript", async () => {
  let releaseOldRoom, reachedOldRoom;
  const enteredOldRoom = new Promise((resolve) => { reachedOldRoom = resolve; });
  const oldRoom = new Promise((_, reject) => { releaseOldRoom = reject; });
  let heldFirstRoom = false;
  const client = instantiate({ roomGate: () => {
    if (heldFirstRoom) return;
    heldFirstRoom = true;
    reachedOldRoom();
    return oldRoom;
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "first-token";
  const oldConnection = client.connect();
  await enteredOldRoom;
  client.disconnect();

  client.get("access-token").value = "second-token";
  await client.connect();
  releaseOldRoom(new Error("stale room response"));
  await oldConnection;
  assert.equal(client.get("world-name").textContent, "test-world");
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
  const transcript = client.get("feed").children.map((item) => item.children[1].children[0].textContent);
  assert.deepEqual(transcript, ["HOST / JOIN"], "old room failure must not appear in new session transcript");
});

test("late command room refresh failure cannot contaminate a rejoined transcript", async () => {
  let releaseOldRoom, reachedOldRoom;
  const enteredOldRoom = new Promise((resolve) => { reachedOldRoom = resolve; });
  const pendingOldRoom = new Promise((_, reject) => { releaseOldRoom = reject; });
  let roomRequests = 0;
  const client = instantiate({ roomGate: () => {
    roomRequests++;
    if (roomRequests === 2) { reachedOldRoom(); return pendingOldRoom; }
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "first-token";
  await client.connect();
  client.get("command-input").value = "look";
  const sending = client.send();
  await enteredOldRoom;
  client.disconnect();
  client.get("access-token").value = "second-token";
  await client.connect();
  releaseOldRoom(new Error("stale command room response"));
  await sending;
  const transcript = client.get("feed").children.map((item) => item.children[1].children[0].textContent);
  assert.deepEqual(transcript, ["HOST / JOIN"], "an old command completion must not write to a new transcript");
});

test("a duplicate connection submit cannot race the first in-flight join", async () => {
  let releaseFirstJoin, reachedFirstJoin;
  const enteredJoin = new Promise((resolve) => { reachedFirstJoin = resolve; });
  const pendingJoin = new Promise((resolve) => { releaseFirstJoin = resolve; });
  let firstJoin = true;
  const client = instantiate({ joinGate: () => {
    if (!firstJoin) return;
    firstJoin = false;
    reachedFirstJoin();
    return pendingJoin;
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  const connecting = client.connect();
  await enteredJoin;
  const secondSubmit = client.connect();
  releaseFirstJoin();
  await Promise.all([connecting, secondSubmit]);
  const joins = client.requests.filter(({ url }) => url.includes("/api/v1/join"));
  assert.equal(joins.length, 1, "only one join must be in flight per browser connection");
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
});

test("a pending join can be cancelled without reviving a disconnected session", async () => {
  let releaseJoin, enteredJoin;
  const waitingForJoin = new Promise((resolve) => { enteredJoin = resolve; });
  const pendingJoin = new Promise((resolve) => { releaseJoin = resolve; });
  const client = instantiate({ joinGate: () => { enteredJoin(); return pendingJoin; } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  const joining = client.connect();
  await waitingForJoin;
  assert.equal(client.get("disconnect-button").disabled, false, "pending joins must be cancellable");
  assert.equal(client.get("access-token").value, "", "pending joins must not leave a secret in the DOM");
  client.disconnect();
  const joinRequest = client.requests.find(({ url }) => url.includes("/api/v1/join"));
  assert.equal(joinRequest.options.signal.aborted, true, "cancel must abort pending join");
  releaseJoin();
  await joining;
  assert.equal(client.get("world-name").textContent, "Unconnected");
  assert.equal(client.get("connection-status").textContent, "Offline — no world connected");
  assert.equal(client.get("connect-button").disabled, false);
  assert.equal(client.timers.length, 0);
});

test("a cancelled old join cannot overwrite a newly connected world", async () => {
  let releaseOldJoin, enteredOldJoin;
  const firstJoinEntered = new Promise((resolve) => { enteredOldJoin = resolve; });
  const oldJoin = new Promise((resolve) => { releaseOldJoin = resolve; });
  let joins = 0;
  const client = instantiate({ joinGate: () => {
    if (++joins === 1) { enteredOldJoin(); return oldJoin; }
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "old-token";
  const original = client.connect();
  await firstJoinEntered;
  client.disconnect();
  client.get("access-token").value = "new-token";
  await client.connect();
  releaseOldJoin();
  await original;
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
  assert.equal(client.get("world-name").textContent, "test-world");
  assert.equal(client.get("access-token").value, "");
  assert.equal(client.get("disconnect-button").disabled, false);
  const transcript = client.get("feed").children.map((item) => item.children[1].children[0].textContent);
  assert.deepEqual(transcript, ["HOST / JOIN"], "old join must not append its own transcript");
  assert.equal(joins, 2);
});

test("a completed old connection cannot re-enable controls during a newer join", async () => {
  let releaseOldRoom, reachedOldRoom, releaseNewJoin, reachedNewJoin;
  const oldRoomEntered = new Promise((resolve) => { reachedOldRoom = resolve; });
  const oldRoom = new Promise((_, reject) => { releaseOldRoom = reject; });
  const newJoinEntered = new Promise((resolve) => { reachedNewJoin = resolve; });
  const newJoin = new Promise((resolve) => { releaseNewJoin = resolve; });
  let roomCalls = 0, joinCalls = 0;
  const client = instantiate({
    roomGate: () => { if (++roomCalls === 1) { reachedOldRoom(); return oldRoom; } },
    joinGate: () => { if (++joinCalls === 2) { reachedNewJoin(); return newJoin; } },
  });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "first-token";
  const first = client.connect();
  await oldRoomEntered;
  client.disconnect();
  client.get("access-token").value = "second-token";
  const second = client.connect();
  await newJoinEntered;
  releaseOldRoom(new Error("old room request failed"));
  await first;
  assert.equal(client.get("connect-button").disabled, true);
  assert.equal(client.get("connect-button").textContent, "Connecting…");
  releaseNewJoin();
  await second;
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
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

test("feed throttling honors bounded Retry-After without disconnecting the session", async () => {
  const clock = { value: 100000 };
  let feedRequests = 0;
  const responses = {
    "/api/v1/health": { ok: true, world_id: "limited-world" },
    "/api/v1/join": { ok: true, text: "Welcome", cursor: 0 },
    "/api/v1/room": { ok: true, text: "The Deck" },
    "/api/v1/map": worldMap(),
    "/api/v1/feed": { ok: true, cursor: 1, events: [{ id: 1, kind: "say", actor: "Tessa", text: "Ready" }] },
  };
  const client = instantiate({ nowMs: clock, fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    assert.ok(route in responses);
    if (route === "/api/v1/feed" && ++feedRequests === 1) {
      return { ok: false, status: 429, headers: { get: () => "5" }, json: async () => ({ error: "Too many requests" }) };
    }
    return { ok: true, status: 200, json: async () => responses[route] };
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  await client.timers[0].fn();
  assert.equal(feedRequests, 1);
  for (let i = 0; i < 4; i++) await client.timers[0].fn();
  assert.equal(feedRequests, 1, "polling must honor the host's retry window");
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
  clock.value += 5001;
  await client.timers[0].fn();
  assert.equal(feedRequests, 2);
  assert.ok(client.get("feed").children.some((item) => item.children[1].children[0].textContent === "Tessa / say"));
});

test("malformed and excessive retry intervals cannot suspend browser feed indefinitely", async () => {
  for (const [header, expectedDelay] of [["invalid", 4400], ["99999", 30000]]) {
    const clock = { value: 100000 };
    let attempts = 0;
    const client = instantiate({ nowMs: clock, fetchImpl: async (url) => {
      const route = new URL(url).pathname;
      if (route === "/api/v1/feed") {
        attempts++;
        if (attempts === 1) return { ok: false, status: 429, headers: { get: () => header }, json: async () => ({ error: "Rate limited" }) };
        return { ok: true, status: 200, json: async () => ({ cursor: 0, events: [] }) };
      }
      const data = route === "/api/v1/health" ? { ok: true, world_id: "host" }
        : route === "/api/v1/join" ? { text: "Hello", cursor: 0 }
          : route === "/api/v1/room" ? { text: "Deck" } : worldMap();
      return { ok: true, status: 200, json: async () => data };
    } });
    client.get("host-url").value = "http://127.0.0.1:8765";
    client.get("player-name").value = "Explorer";
    client.get("access-token").value = "limited-token";
    await client.connect();
    await client.timers[0].fn();
    clock.value += expectedDelay - 1;
    await client.timers[0].fn();
    assert.equal(attempts, 1, `must wait for bounded retry interval (${header})`);
    clock.value++;
    await client.timers[0].fn();
    assert.equal(attempts, 2, `must resume after bounded retry interval (${header})`);
    client.disconnect();
  }
});

test("HTTP 503 and 408 feed failures back off without immediately losing the world", async () => {
  const clock = { value: 100000 };
  let feedCalls = 0;
  const client = instantiate({ nowMs: clock, fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/feed") {
      feedCalls++;
      if (feedCalls === 1) {
        return { ok: false, status: 503, headers: { get: () => null },
          json: async () => { throw new SyntaxError("upstream sent an empty overload body"); } };
      }
      if (feedCalls === 2) {
        return { ok: false, status: 408, json: async () => ({ error: "Request timed out" }) };
      }
      return { ok: true, status: 200,
        json: async () => ({ cursor: 7, events: [{ id: 7, actor: "Crewmate", kind: "say", text: "Back online" }] }) };
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "busy-world" }
      : route === "/api/v1/join" ? { text: "Welcome", cursor: 0 }
        : route === "/api/v1/room" ? { text: "The Deck" } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  await client.timers[0].fn();
  assert.equal(feedCalls, 1);
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
  assert.ok(client.get("feed").children.some((item) =>
    /HOST \/ BUSY/.test(item.children[1].children[0].textContent)));
  clock.value += 4399;
  await client.timers[0].fn();
  assert.equal(feedCalls, 1, "a busy host must not be hammered on every polling interval");
  clock.value++;
  await client.timers[0].fn();
  assert.equal(feedCalls, 2);
  clock.value += 8800;
  await client.timers[0].fn();
  assert.equal(feedCalls, 3);
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
  assert.ok(client.get("feed").children.some((item) =>
    item.children[1].children[0].textContent === "Crewmate / say"));
});

test("repeated host overload fails closed after a bounded number of poll retries", async () => {
  const clock = { value: 1000 };
  let feedCalls = 0;
  const client = instantiate({ nowMs: clock, fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/feed") {
      feedCalls++;
      return { ok: false, status: 503, json: async () => ({ error: "Server busy" }) };
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "busy-world" }
      : route === "/api/v1/join" ? { text: "Welcome", cursor: 0 }
        : route === "/api/v1/room" ? { text: "The Deck" } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  for (let i = 0; i < 4; i++) {
    await client.timers[0].fn();
    clock.value += 31000;
  }
  assert.equal(feedCalls, 4, "the temporary-backoff window must be bounded");
  assert.equal(client.get("world-name").textContent, "Unconnected");
  assert.match(client.get("connection-note").textContent, /busy repeatedly/i);
});

test("HTTP 503/408 commands retain the same action ID for explicit manual retry", async () => {
  const actionIds = [];
  const client = instantiate({ fetchImpl: async (url, options) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/command") {
      const command = JSON.parse(options.body);
      actionIds.push(command.action_id);
      if (actionIds.length === 1) return { ok: false, status: 503,
        json: async () => ({ error: "Server busy" }) };
      if (actionIds.length === 2) return { ok: false, status: 408,
        json: async () => ({ error: "Body timed out" }) };
      return { ok: true, status: 200, json: async () => ({ text: "Action accepted" }) };
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "busy-world" }
      : route === "/api/v1/join" ? { text: "Welcome", cursor: 0 }
        : route === "/api/v1/room" ? { text: "The Deck" }
          : route === "/api/v1/feed" ? { cursor: 0, events: [] } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  client.get("command-input").value = "say Hold position";
  await client.send();
  assert.equal(client.get("command-input").value, "say Hold position");
  assert.equal(actionIds.length, 1, "never automatically replay a possibly committed command");
  await client.send();
  assert.equal(client.get("command-input").value, "say Hold position");
  assert.equal(actionIds.length, 2);
  await client.send();
  assert.equal(client.get("command-input").value, "");
  assert.equal(actionIds.length, 3);
  assert.equal(actionIds[0], actionIds[1]);
  assert.equal(actionIds[1], actionIds[2]);
  assert.ok(client.get("feed").children.some((item) =>
    /HOST \/ RETRY/.test(item.children[1].children[0].textContent)));
});

test("editing a failed command creates a distinct action ID", async () => {
  const sent = [];
  const client = instantiate({ fetchImpl: async (url, options) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/command") {
      const body = JSON.parse(options.body);
      sent.push(body);
      if (sent.length === 1) return { ok: false, status: 503,
        json: async () => ({ error: "Busy" }) };
      return { ok: true, status: 200, json: async () => ({ text: "Accepted" }) };
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "edit-world" }
      : route === "/api/v1/join" ? { text: "Welcome", cursor: 0 }
        : route === "/api/v1/room" ? { text: "The Deck" }
          : route === "/api/v1/feed" ? { cursor: 0, events: [] } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  client.get("command-input").value = "say First";
  await client.send();
  client.get("command-input").value = "say Revised";
  await client.send();
  assert.equal(sent.length, 2);
  assert.notEqual(sent[0].action_id, sent[1].action_id);
  assert.equal(sent[1].command, "say Revised");
});

test("duplicate Send event while a command is in flight never submits twice", async () => {
  let releaseCommand, reachedCommand;
  const entered = new Promise((resolve) => { reachedCommand = resolve; });
  const pending = new Promise((resolve) => { releaseCommand = resolve; });
  let sent = 0;
  const client = instantiate({ fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/command") {
      sent++;
      reachedCommand();
      await pending;
      return { ok: true, status: 200, json: async () => ({ text: "Executed once" }) };
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "busy-world" }
      : route === "/api/v1/join" ? { text: "Welcome", cursor: 0 }
        : route === "/api/v1/room" ? { text: "The Deck" }
          : route === "/api/v1/feed" ? { cursor: 0, events: [] } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  client.get("command-input").value = "say Hold";
  const first = client.send();
  await entered;
  await client.send();
  assert.equal(sent, 1);
  releaseCommand();
  await first;
  assert.equal(client.get("command-input").value, "");
});

test("a late 503 command response cannot alter a freshly rejoined session", async () => {
  let releaseOld, reachedOld;
  const waiting = new Promise((resolve) => { reachedOld = resolve; });
  const held = new Promise((resolve) => { releaseOld = resolve; });
  let commands = 0;
  const client = instantiate({ fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/command") {
      if (++commands === 1) { reachedOld(); await held; }
      return { ok: false, status: 503, json: async () => ({ error: "Server busy" }) };
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "rejoined-world" }
      : route === "/api/v1/join" ? { text: "Welcome", cursor: 0 }
        : route === "/api/v1/room" ? { text: "The Deck" }
          : route === "/api/v1/feed" ? { cursor: 0, events: [] } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "old-token";
  await client.connect();
  client.get("command-input").value = "north";
  const oldSend = client.send();
  await waiting;
  client.disconnect();
  client.get("access-token").value = "new-token";
  await client.connect();
  releaseOld();
  await oldSend;
  assert.equal(client.get("world-name").textContent, "rejoined-world");
  assert.equal(client.get("command-input").value, "");
  assert.deepEqual(client.get("feed").children.map((item) =>
    item.children[1].children[0].textContent), ["HOST / JOIN"]);
});

const gatewayRoot = process.env.REALM_LIVE_GATEWAY_ROOT || path.join(__dirname, "../..");
const gatewaySource = path.join(gatewayRoot, "src/realm/gateway.py");

test("live Python gateway: browser join, authored map, movement, and token rejection", {
  skip: !fs.existsSync(gatewaySource) && "Gateway not present on this stacked browser-only branch",
}, async (t) => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "realm-browser-gateway-"));
  const db = path.join(temp, "world.sqlite3");
  const token = "local-protocol-test-token";
  const fixture = path.join(__dirname, "live-gateway-fixture.py");
  const child = spawn(process.env.REALM_PYTHON || "python", [fixture, db, token], {
    cwd: gatewayRoot,
    env: { ...process.env, PYTHONPATH: path.join(gatewayRoot, "src") },
    stdio: ["ignore", "pipe", "pipe"],
  });
  t.after(() => {
    child.kill();
    fs.rmSync(temp, { recursive: true, force: true });
  });
  let stderr = "";
  child.stderr.on("data", (data) => { stderr += data.toString(); });
  const ready = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Gateway startup timed out: " + stderr)), 9000);
    child.once("exit", (code) => { clearTimeout(timeout); reject(new Error(`Gateway exited ${code}: ${stderr}`)); });
    child.stdout.on("data", (data) => {
      const match = data.toString().match(/READY (\d+) ([A-Za-z0-9_-]+) ([A-Za-z0-9_-]+) (legacy|player)/);
      if (match) { clearTimeout(timeout); resolve({ port: Number(match[1]), playerToken: match[2], otherToken: match[3], mode: match[4] }); }
    });
  });
  const base = `http://127.0.0.1:${ready.port}`;
  const liveFetch = (url, options) => globalThis.fetch(url, {
    ...options,
    headers: { ...options.headers, Origin: "http://127.0.0.1:8000" },
  });
  const client = instantiate({ fetchImpl: liveFetch });
  client.get("host-url").value = base;
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = ready.playerToken;
  await client.connect();
  assert.equal(client.get("world-name").textContent, "browser-integration-test");
  assert.equal(client.get("map-status").textContent, "LIVE HOST MAP · 3×2");
  assert.equal(client.get("map-position").textContent, "@ 0,0");
  client.get("command-input").value = "north";
  await client.send();
  assert.equal(client.get("map-position").textContent, "@ 2,0");
  assert.equal(client.get("command-input").value, "");
  client.disconnect();
  assert.equal(client.get("map-status").textContent, "MAP UNAVAILABLE");
  const intruder = instantiate({ fetchImpl: liveFetch });
  intruder.get("host-url").value = base;
  intruder.get("player-name").value = "Explorer";
  intruder.get("access-token").value = "incorrect-token";
  await intruder.connect();
  assert.equal(intruder.get("world-name").textContent, "Unconnected");
  assert.match(intruder.get("connection-note").textContent, /401/);
  if (ready.mode === "player") {
    const wrongActor = instantiate({ fetchImpl: liveFetch });
    wrongActor.get("host-url").value = base;
    wrongActor.get("player-name").value = "Explorer";
    wrongActor.get("access-token").value = ready.otherToken;
    await wrongActor.connect();
    assert.equal(wrongActor.get("world-name").textContent, "Unconnected");
    assert.match(wrongActor.get("connection-note").textContent, /403/);
  }
});
