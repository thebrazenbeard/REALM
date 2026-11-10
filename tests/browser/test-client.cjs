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

function instantiate({ missingMap = false, mapOnMove = false, rejectUnauthorized = false, fetchImpl = null, mapGate = null, roomGate = null, joinGate = null, commandGate = null } = {}) {
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
    if (route === "/api/v1/command" && commandGate) await commandGate();
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

test("command history recalls prior commands, restores draft, and clears on disconnect", async () => {
  const client = instantiate();
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();

  for (const command of ["look", "who"]) {
    client.get("command-input").value = command;
    await client.send();
  }

  const input = client.get("command-input");
  function press(key) {
    let prevented = false;
    input.events.keydown({ key, preventDefault() { prevented = true; } });
    return prevented;
  }
  input.value = "unfinished message";
  assert.equal(press("ArrowUp"), true);
  assert.equal(input.value, "who");
  assert.equal(press("ArrowUp"), true);
  assert.equal(input.value, "look");
  assert.equal(press("ArrowUp"), true, "oldest command stays selected");
  assert.equal(input.value, "look");
  assert.equal(press("ArrowDown"), true);
  assert.equal(input.value, "who");
  assert.equal(press("ArrowDown"), true);
  assert.equal(input.value, "unfinished message");
  assert.equal(press("ArrowDown"), false, "ordinary key behavior when not browsing history");

  client.disconnect();
  client.get("access-token").value = "new-token";
  await client.connect();
  input.value = "fresh session";
  assert.equal(press("ArrowUp"), false, "old command history must not survive reconnect");
  assert.equal(input.value, "fresh session");
});

test("command recall retains only 50 entries and editing begins a fresh history search", async () => {
  const client = instantiate();
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  const input = client.get("command-input");
  for (let i = 0; i < 53; i++) {
    input.value = "look-" + i;
    await client.send();
  }
  for (let i = 0; i < 55; i++) {
    input.events.keydown({ key: "ArrowUp", preventDefault() {} });
  }
  assert.equal(input.value, "look-3", "older commands must be evicted from the in-memory ring");
  input.value = "edited draft";
  input.events.input({});
  input.events.keydown({ key: "ArrowUp", preventDefault() {} });
  assert.equal(input.value, "look-52", "editing resets recall to the latest command");
  input.events.keydown({ key: "ArrowDown", preventDefault() {} });
  assert.equal(input.value, "edited draft", "new draft is preserved after editing");
  client.disconnect();
});

test("rapid duplicate command submits issue only one POST and one action ID", async () => {
  let reachedCommand, releaseCommand;
  const enteredCommand = new Promise((resolve) => { reachedCommand = resolve; });
  const heldCommand = new Promise((resolve) => { releaseCommand = resolve; });
  const client = instantiate({ commandGate: () => { reachedCommand(); return heldCommand; } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();

  client.get("command-input").value = "say This should happen only once";
  const first = client.send();
  await enteredCommand;
  assert.equal(client.get("send-button").disabled, true);
  const second = client.send(); // Synthetic double-submit bypassing the disabled DOM.
  await Promise.resolve();
  const posts = client.requests.filter(({ url }) => url.endsWith("/api/v1/command"));
  assert.equal(posts.length, 1, "disabled controls alone do not guard the async handler");
  assert.equal(typeof JSON.parse(posts[0].options.body).action_id, "string");

  releaseCommand();
  await Promise.all([first, second]);
  assert.equal(client.get("send-button").disabled, false);
  assert.equal(client.get("command-input").value, "");
});

test("an old pending command cannot block or change a reconnected session", async () => {
  let releaseOldCommand, reachedOldCommand;
  const enteredOldCommand = new Promise((resolve) => { reachedOldCommand = resolve; });
  const heldOldCommand = new Promise((resolve) => { releaseOldCommand = resolve; });
  let commandCount = 0;
  const client = instantiate({ commandGate: () => {
    commandCount++;
    if (commandCount === 1) { reachedOldCommand(); return heldOldCommand; }
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "old-token";
  await client.connect();

  client.get("command-input").value = "say Old command";
  const oldCommand = client.send();
  await enteredOldCommand;
  client.disconnect();
  client.get("access-token").value = "new-token";
  await client.connect();
  client.get("command-input").value = "look";
  await client.send();
  assert.equal(commandCount, 2, "new session must not inherit an old pending guard");
  assert.equal(client.get("send-button").disabled, false);

  releaseOldCommand();
  await oldCommand;
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
  assert.equal(client.get("command-input").disabled, false);
  assert.equal(client.get("send-button").disabled, false);
  const transcript = client.get("feed").children.map((item) => item.children[1].children[0].textContent);
  assert.ok(transcript.some((label) => label.includes("Explorer / look")));
  assert.ok(!transcript.some((label) => label.includes("Old command")), "old completion cannot contaminate the new feed");
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
