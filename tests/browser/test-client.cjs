"use strict";

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
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

function instantiate({ missingMap = false, mapOnMove = false, rejectUnauthorized = false, fetchImpl = null, mapGate = null, roomGate = null, joinGate = null, healthGate = null, commandGate = null, quickActionCommands = [], cryptoImpl = require("node:crypto").webcrypto, nowMs = null, expectedWorld = "test-world", joinResponse = null } = {}) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  get("expected-world-id").value = expectedWorld;
  const requests = [];
  const timers = [];
  let mapAvailable = !missingMap;
  const document = {
    getElementById: get,
    querySelectorAll: (selector) => selector === "[data-command]" ? quickActionCommands.map((command) => {
      const button = get("quick-" + command);
      button.dataset.command = command;
      return button;
    }) : [],
    createElement: () => new Element(),
    createDocumentFragment: () => new Element(),
  };
  const responses = {
    "/api/v1/health": { ok: true, world_id: "test-world" },
    "/api/v1/join": joinResponse || { text: "Welcome", cursor: 0 },
    "/api/v1/room": { text: "You are in a field by water." },
    "/api/v1/map": worldMap(),
    "/api/v1/feed": { cursor: 0, events: [] },
    "/api/v1/command": { text: "You move north", cursor: 0 },
  };
  const mockFetch = async (url, options) => {
    requests.push({ url, options });
    const route = new URL(url).pathname;
    if (route === "/api/v1/health" && healthGate) await healthGate();
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
    crypto: cryptoImpl, AbortController,
    Date: nowMs ? class extends Date { static now() { return nowMs.value; } } : Date,
  }, { filename: "docs/app.js" });
  const event = { preventDefault() {} };
  return { get, requests, timers, connect: () => get("connection-form").events.submit(event),
    disconnect: () => get("disconnect-button").events.click(),
    send: () => get("command-form").events.submit(event) };
}

test("expected world ID gates a browser join and stays fixed while connected", async () => {
  const client = instantiate();
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  assert.equal(client.get("world-name").textContent, "test-world");
  assert.ok(client.requests.some(({ url }) => url.endsWith("/api/v1/join")));
  assert.equal(client.get("expected-world-id").disabled, true);
  client.disconnect();
  assert.equal(client.get("expected-world-id").disabled, false);
});

test("wrong expected world ID refuses bearer authentication after an unauthenticated health probe", async () => {
  const client = instantiate();
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("expected-world-id").value = "wrong-world";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  assert.equal(client.get("world-name").textContent, "Unconnected");
  assert.equal(client.requests.length, 1);
  assert.match(client.requests[0].url, /\/api\/v1\/health$/);
  assert.equal(client.requests[0].options.headers.Authorization, undefined);
  assert.equal(client.get("access-token").value, "");
  assert.match(client.get("connection-note").textContent, /World mismatch/);
});

test("an authenticated browser join never follows an HTTP redirect with its bearer", async () => {
  const observed = [];
  const server = http.createServer((req, res) => {
    observed.push({ path: req.url, authorization: req.headers.authorization });
    res.setHeader("Content-Type", "application/json");
    if (req.url === "/api/v1/health") return res.end(JSON.stringify({ ok: true, world_id: "test-world" }));
    if (req.url === "/api/v1/join") {
      res.writeHead(307, { Location: "/unexpected-join-handler" });
      return res.end();
    }
    if (req.url === "/unexpected-join-handler") return res.end(JSON.stringify({ cursor: 0, text: "Redirected join" }));
    if (req.url.startsWith("/api/v1/room")) return res.end(JSON.stringify({ text: "A room." }));
    res.writeHead(404);
    return res.end(JSON.stringify({ error: "Not found" }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const client = instantiate({ fetchImpl: fetch });
  client.get("host-url").value = "http://127.0.0.1:" + server.address().port;
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  try {
    await client.connect();
    assert.equal(observed[0].path, "/api/v1/health");
    assert.equal(observed[0].authorization, undefined);
    assert.equal(observed[1].path, "/api/v1/join");
    assert.equal(observed[1].authorization, "Bearer secret-token");
    assert.equal(observed.some((item) => item.path === "/unexpected-join-handler"), false,
      "The browser may not forward a bearer POST to a redirect target");
    assert.equal(client.get("world-name").textContent, "Unconnected");
  } finally {
    client.disconnect();
    await new Promise((resolve) => server.close(resolve));
  }
});

test("editing the expected world while health is pending cannot redirect an in-flight join", async () => {
  let releaseHealth;
  let reachedHealth;
  const pending = new Promise((resolve) => { releaseHealth = resolve; });
  const entered = new Promise((resolve) => { reachedHealth = resolve; });
  const client = instantiate({ expectedWorld: "wrong-world", healthGate: async () => {
    reachedHealth();
    await pending;
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  const attempt = client.connect();
  await entered;
  client.get("expected-world-id").value = "test-world";
  releaseHealth();
  await attempt;
  assert.equal(client.requests.length, 1);
  assert.match(client.requests[0].url, /\/api\/v1\/health$/);
  assert.equal(client.requests[0].options.headers.Authorization, undefined);
  assert.match(client.get("connection-note").textContent, /expected wrong-world/);
  assert.equal(client.get("access-token").value, "");
});

test("missing or invalid expected world ID rejects locally with no network requests", async () => {
  for (const expected of ["   ", "a world", "1wrong", "a".repeat(65)]) {
    const client = instantiate();
    client.get("host-url").value = "http://127.0.0.1:8765";
    client.get("expected-world-id").value = expected;
    client.get("player-name").value = "Explorer";
    client.get("access-token").value = "secret-token";
    await client.connect();
    assert.equal(client.requests.length, 0, "unbound world " + JSON.stringify(expected));
    assert.notEqual(client.get("world-name").textContent, "test-world");
  }
});


test("browser uses host room-entry cursor instead of later global join head", async () => {
  const client = instantiate({ joinResponse: {
    ok: true, text: "Welcome", cursor: 11, room_entry_cursor: 6,
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  assert.equal(client.timers.length, 1);
  await client.timers[0].fn();
  const feed = client.requests.find(({ url }) => url.includes("/api/v1/feed?"));
  assert.ok(feed, "browser must fetch room feed");
  assert.equal(new URL(feed.url).searchParams.get("after"), "6",
    "global cursor 11 would lose valid post-entry speech");
});

test("browser falls back to global cursor only for legacy host omission", async () => {
  const client = instantiate({ joinResponse: { text: "Old host", cursor: 11 } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  await client.timers[0].fn();
  const feed = client.requests.find(({ url }) => url.includes("/api/v1/feed?"));
  assert.equal(new URL(feed.url).searchParams.get("after"), "11");
});

test("browser rejects malformed or future room-entry cursors rather than trusting them", async () => {
  for (const cursor of [-1, 12, "6", null, 1.25]) {
    const client = instantiate({ joinResponse: {
      ok: true, text: "Invalid host", cursor: 11, room_entry_cursor: cursor,
    } });
    client.get("host-url").value = "http://127.0.0.1:8765";
    client.get("player-name").value = "Explorer";
    client.get("access-token").value = "secret-token";
    await client.connect();
    assert.equal(client.get("world-name").textContent, "Unconnected");
    assert.match(client.get("connection-note").textContent, /room.entry cursor/i);
  }
});

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
  assert.equal(press("ArrowUp"), true, "recalling past the oldest entry stays at the oldest");
  assert.equal(input.value, "look");
  assert.equal(press("ArrowDown"), true);
  assert.equal(input.value, "who");
  assert.equal(press("ArrowDown"), true);
  assert.equal(input.value, "unfinished message");
  assert.equal(press("ArrowDown"), false);
  client.disconnect();
  client.get("access-token").value = "new-token";
  await client.connect();
  input.value = "fresh session";
  assert.equal(press("ArrowUp"), false);
  assert.equal(input.value, "fresh session");
});

test("command history keeps only 50 entries and restarts recall after typing", async () => {
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
  for (let i = 0; i < 55; i++) input.events.keydown({ key: "ArrowUp", preventDefault() {} });
  assert.equal(input.value, "look-3");
  input.value = "edited draft";
  input.events.input({});
  input.events.keydown({ key: "ArrowUp", preventDefault() {} });
  assert.equal(input.value, "look-52");
  input.events.keydown({ key: "ArrowDown", preventDefault() {} });
  assert.equal(input.value, "edited draft");
  client.disconnect();
});

test("suggested command replaces stale history navigation instead of being overwritten", async () => {
  const client = instantiate({ quickActionCommands: ["inventory"] });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "synthetic-token";
  await client.connect();
  for (const command of ["look", "who"]) {
    client.get("command-input").value = command;
    await client.send();
  }
  const input = client.get("command-input");
  input.value = "unfinished draft";
  input.events.input({});
  input.events.keydown({ key: "ArrowUp", preventDefault() {} });
  assert.equal(input.value, "who");
  client.get("quick-inventory").events.click();
  assert.equal(input.value, "inventory");
  input.events.keydown({ key: "ArrowDown", preventDefault() {} });
  assert.equal(input.value, "inventory", "old draft must not replace the chosen suggestion");
  input.events.keydown({ key: "ArrowUp", preventDefault() {} });
  assert.equal(input.value, "who", "new recall should start from most recent history entry");
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

  // A synthetic synchronous reentry during UI disable must see the pending
  // latch already set; disabled controls alone cannot enforce this invariant.
  const sendButton = client.get("send-button");
  let disabled = sendButton.disabled;
  let reentered = false;
  const reentrantSends = [];
  Object.defineProperty(sendButton, "disabled", {
    configurable: true,
    get: () => disabled,
    set(next) {
      disabled = next;
      if (next && !reentered) {
        reentered = true;
        reentrantSends.push(client.send());
      }
    },
  });
  client.get("command-input").value = "say This should happen only once";
  const first = client.send();
  await enteredCommand;
  assert.equal(sendButton.disabled, true);
  const second = client.send();
  await Promise.resolve();
  const posts = client.requests.filter(({ url }) => url.endsWith("/api/v1/command"));
  releaseCommand();
  await Promise.all([first, second, ...reentrantSends]);
  assert.equal(posts.length, 1, "synchronous and asynchronous reentry must not duplicate a POST");
  assert.equal(typeof JSON.parse(posts[0].options.body).action_id, "string");
  assert.equal(sendButton.disabled, false);
  assert.equal(client.get("command-input").value, "");
});

test("a synchronous reentrant command value getter cannot double-submit", async () => {
  const client = instantiate();
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  const input = client.get("command-input");
  let value = "say Only once";
  let reentered = false;
  const nested = [];
  Object.defineProperty(input, "value", {
    configurable: true,
    get() {
      if (!reentered) {
        reentered = true;
        nested.push(client.send());
      }
      return value;
    },
    set(next) { value = next; },
  });
  await client.send();
  await Promise.all(nested);
  const posts = client.requests.filter(({ url }) => url.endsWith("/api/v1/command"));
  assert.equal(posts.length, 1, "the pending latch must precede any potentially reentrant input getter");
  assert.equal(input.value, "");
});

test("a throwing command value getter releases the pending latch for retry", async () => {
  const client = instantiate();
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  const input = client.get("command-input");
  let value = "look";
  let failOnce = true;
  Object.defineProperty(input, "value", {
    configurable: true,
    get() {
      if (failOnce) { failOnce = false; throw new Error("synthetic value getter failure"); }
      return value;
    },
    set(next) { value = next; },
  });
  await assert.rejects(client.send(), /synthetic value getter failure/);
  assert.equal(client.requests.filter(({ url }) => url.endsWith("/api/v1/command")).length, 0);
  value = "   ";
  await client.send();
  assert.equal(client.requests.filter(({ url }) => url.endsWith("/api/v1/command")).length, 0, "blank command is a no-op");
  value = "look";
  await client.send();
  assert.equal(client.requests.filter(({ url }) => url.endsWith("/api/v1/command")).length, 1);
  assert.equal(input.value, "");
});

test("a throwing disabled setter after latch cannot permanently block manual retry", async () => {
  const client = instantiate();
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  const sendButton = client.get("send-button");
  let disabled = sendButton.disabled;
  let failOnce = true;
  Object.defineProperty(sendButton, "disabled", {
    configurable: true,
    get: () => disabled,
    set(next) {
      if (next && failOnce) { failOnce = false; throw new Error("synthetic disabled setter failure"); }
      disabled = next;
    },
  });
  client.get("command-input").value = "say Recover safely";
  await assert.rejects(client.send(), /synthetic disabled setter failure/);
  assert.equal(client.requests.filter(({ url }) => url.endsWith("/api/v1/command")).length, 0);
  await client.send();
  assert.equal(client.requests.filter(({ url }) => url.endsWith("/api/v1/command")).length, 1);
  assert.equal(sendButton.disabled, false);
});

test("a throwing UUID generator cannot poison latch or record an unsent command", async () => {
  let failOnce = true;
  const client = instantiate({ cryptoImpl: { randomUUID() {
    if (failOnce) { failOnce = false; throw new Error("synthetic UUID failure"); }
    return "unique-retry-action-id";
  } } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  const input = client.get("command-input");
  input.value = "say Retry after setup failure";
  await assert.rejects(client.send(), /synthetic UUID failure/);
  assert.equal(client.requests.filter(({ url }) => url.endsWith("/api/v1/command")).length, 0);
  input.value = "unsent draft";
  let prevented = false;
  input.events.keydown({ key: "ArrowUp", preventDefault() { prevented = true; } });
  assert.equal(prevented, false, "pre-dispatch setup failure must not enter command history");
  assert.equal(input.value, "unsent draft");
  input.value = "say Retry after setup failure";
  await client.send();
  const posts = client.requests.filter(({ url }) => url.endsWith("/api/v1/command"));
  assert.equal(posts.length, 1);
  assert.equal(JSON.parse(posts[0].options.body).action_id, "unique-retry-action-id");
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
  assert.equal(commandCount, 2, "new session must not inherit old pending state");
  assert.equal(client.get("send-button").disabled, false);

  releaseOldCommand();
  await oldCommand;
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
  assert.equal(client.get("command-input").disabled, false);
  assert.equal(client.get("send-button").disabled, false);
  const transcript = client.get("feed").children.map((item) => item.children[1].children[0].textContent);
  assert.ok(transcript.some((label) => label.includes("Explorer / look")));
  assert.ok(!transcript.some((label) => label.includes("Old command")), "old completion cannot contaminate new feed");
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
  const client = instantiate({ expectedWorld: "race-world", nowMs: clock, fetchImpl: async (url) => {
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
  const client = instantiate({ expectedWorld: "room-race", fetchImpl: async (url) => {
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
  assert.equal(client.get("disconnect-button").textContent, "Cancel", "pending control must describe its purpose");
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

test("a pending health check can be cancelled before any join, even if response arrives late", async () => {
  let releaseHealth, enteredHealth;
  const waiting = new Promise((resolve) => { enteredHealth = resolve; });
  const pending = new Promise((resolve) => { releaseHealth = resolve; });
  const client = instantiate({ healthGate: () => { enteredHealth(); return pending; } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  const connecting = client.connect();
  await waiting;
  assert.equal(client.get("disconnect-button").textContent, "Cancel");
  const healthRequest = client.requests.find(({ url }) => url.endsWith("/api/v1/health"));
  client.disconnect();
  assert.equal(healthRequest.options.signal.aborted, true);
  releaseHealth();
  await connecting;
  assert.equal(client.requests.filter(({ url }) => url.endsWith("/api/v1/join")).length, 0);
  assert.equal(client.get("connection-status").textContent, "Offline — no world connected");
  assert.equal(client.get("access-token").value, "");
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

test("HTTP 200 join without an authoritative success cursor never connects", async () => {
  const invalid = [
    {}, { text: "Welcome" }, { cursor: null }, { cursor: true }, { cursor: -1 },
    { cursor: 1.25 }, { cursor: "7" }, { ok: false, cursor: 7 },
  ];
  for (const body of invalid) {
    const routes = [];
    const client = instantiate({ fetchImpl: async (url) => {
      const route = new URL(url).pathname;
      routes.push(route);
      const data = route === "/api/v1/health" ? { ok: true, world_id: "test-world" }
        : route === "/api/v1/join" ? body : { text: "Should never load" };
      return { ok: true, status: 200, json: async () => data };
    } });
    client.get("host-url").value = "http://localhost:8765";
    client.get("player-name").value = "Explorer";
    client.get("access-token").value = "secret-token";
    await client.connect();
    assert.equal(client.get("world-name").textContent, "Unconnected", JSON.stringify(body));
    assert.equal(client.get("command-input").disabled, true);
    assert.equal(client.get("access-token").value, "");
    assert.match(client.get("connection-note").textContent, /join response/i);
    assert.deepEqual(routes, ["/api/v1/health", "/api/v1/join"], "no post-join reads after malformed success");
    assert.equal(client.timers.length, 0);
  }
});

test("an inherited prototype cursor cannot turn an empty HTTP 200 join into a session", async () => {
  const original = Object.getOwnPropertyDescriptor(Object.prototype, "cursor");
  try {
    Object.defineProperty(Object.prototype, "cursor", { value: 3, configurable: true });
    const body = JSON.parse("{}");
    assert.equal(Object.prototype.hasOwnProperty.call(body, "cursor"), false);
    assert.equal(body.cursor, 3, "the test reproduces a polluted JSON prototype");
    const routes = [];
    const client = instantiate({ fetchImpl: async (url) => {
      const route = new URL(url).pathname;
      routes.push(route);
      const data = route === "/api/v1/health" ? { ok: true, world_id: "test-world" }
        : route === "/api/v1/join" ? body : { text: "Should never load" };
      return { ok: true, status: 200, json: async () => data };
    } });
    client.get("host-url").value = "http://localhost:8765";
    client.get("player-name").value = "Explorer";
    client.get("access-token").value = "secret-token";
    await client.connect();
    assert.equal(client.get("world-name").textContent, "Unconnected");
    assert.match(client.get("connection-note").textContent, /join response/i);
    assert.deepEqual(routes, ["/api/v1/health", "/api/v1/join"]);
  } finally {
    if (original) Object.defineProperty(Object.prototype, "cursor", original);
    else delete Object.prototype.cursor;
  }
});

test("partial legacy-compatible join with a valid cursor still connects", async () => {
  for (const accepted of [{ cursor: 3 }, { ok: true, cursor: 0 }]) {
    const client = instantiate({ fetchImpl: async (url) => {
      const route = new URL(url).pathname;
      const data = route === "/api/v1/health" ? { ok: true, world_id: "test-world" }
        : route === "/api/v1/join" ? accepted
          : route === "/api/v1/room" ? { text: "A room" }
            : route === "/api/v1/feed" ? { cursor: accepted.cursor, events: [] } : worldMap();
      return { ok: true, status: 200, json: async () => data };
    } });
    client.get("host-url").value = "http://localhost:8765";
    client.get("player-name").value = "Explorer";
    client.get("access-token").value = "secret-token";
    await client.connect();
    assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
    assert.ok(client.get("feed").children.some((item) =>
      item.children[1].children[1].textContent === "Connected to host."));
  }
});

test("late malformed join from cancelled session cannot undo a new valid join", async () => {
  let entered, release;
  const oldEntered = new Promise((resolve) => { entered = resolve; });
  const oldHeld = new Promise((resolve) => { release = resolve; });
  let joins = 0;
  const client = instantiate({ expectedWorld: "new-world", fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/join" && ++joins === 1) {
      entered();
      await oldHeld;
      return { ok: true, status: 200, json: async () => ({}) };
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "new-world" }
      : route === "/api/v1/join" ? { ok: true, cursor: 5 }
        : route === "/api/v1/room" ? { text: "New room" }
          : route === "/api/v1/feed" ? { events: [], cursor: 5 } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "old-token";
  const previous = client.connect();
  await oldEntered;
  client.disconnect();
  client.get("access-token").value = "new-token";
  await client.connect();
  release();
  await previous;
  assert.equal(joins, 2);
  assert.equal(client.get("world-name").textContent, "new-world");
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
  assert.equal(client.get("connection-note").classList.contains("is-error"), false);
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
  const client = instantiate({ expectedWorld: "limited-world", nowMs: clock, fetchImpl: async (url) => {
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

test("global PvP death notices render as distinct inert heralds without exposing a room", async () => {
  const notice = "Hear the iron bell: Moss has fallen before Ember. <img src=x onerror=alert(1)>";
  const responses = {
    "/api/v1/health": { ok: true, world_id: "fantasy-world" },
    "/api/v1/join": { ok: true, text: "Welcome", cursor: 0 },
    "/api/v1/room": { ok: true, text: "The Atrium" },
    "/api/v1/map": worldMap(),
    "/api/v1/feed": { ok: true, cursor: 3, events: [
      { id: 2, actor: "Ember", kind: "pvp_death", room: "global", text: notice,
        payload_json: "secret fight details", private_room: "Hidden Crypt" },
      { id: 3, actor: "World", kind: "say", room: "atrium", text: "The wind settles." },
    ] },
  };
  const client = instantiate({ expectedWorld: "fantasy-world", fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    assert.ok(route in responses, "unexpected route " + route);
    return { ok: true, status: 200, json: async () => responses[route] };
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  await client.timers[0].fn();
  const herald = client.get("feed").children.find((item) => item.children[1].children[1].textContent === notice);
  assert.ok(herald, "the server's original message must be rendered as inert textContent");
  assert.match(herald.className, /feed-pvp-death/, "PK notice requires a styleable dedicated category");
  assert.match(herald.className, /feed-response/, "PK notice should be visibly distinct using existing gold treatment");
  assert.equal(herald.children[1].children[0].textContent, "REALM / DEATH HERALD");
  assert.equal(herald.children[0].textContent, "⚔");
  assert.ok(!herald.children[1].children[0].textContent.includes("atrium"));
  assert.ok(!client.get("feed").children.some((item) => item.children[1].children[1].textContent.includes("Hidden Crypt")));
  assert.ok(client.get("feed").children.some((item) => item.children[1].children[0].textContent === "World / say · atrium"),
    "normal room-local feed formatting must remain unchanged");
});

test("malformed PvP death event never echoes a private room identifier in its title", async () => {
  const client = instantiate({ expectedWorld: "fantasy-world", fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    const payloads = {
      "/api/v1/health": { ok: true, world_id: "fantasy-world" },
      "/api/v1/join": { ok: true, text: "Welcome", cursor: 0 },
      "/api/v1/room": { ok: true, text: "The Atrium" },
      "/api/v1/map": worldMap(),
      "/api/v1/feed": { ok: true, cursor: 2, events: [
        { id: 2, actor: "Ember", kind: "pvp_death", room: "secret-corridor", text: "A duel ended." },
      ] },
    };
    return { ok: true, status: 200, json: async () => payloads[route] };
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  await client.timers[0].fn();
  const titles = client.get("feed").children.map((item) => item.children[1].children[0].textContent);
  assert.ok(!titles.some((title) => title.includes("secret-corridor")));
});

test("emote, OOC, and private group messages render with fixed headings and inert host text", async () => {
  const hostText = {
    emote: 'Moss bows beneath the moon. <img src=x onerror=alert(1)>',
    ooc: '[OOC Channel] Fenn: Where is the tavern? <script>evil()</script>',
    crewchat: '[The Free Company][Scout] Fenn: "The west gate is open."',
  };
  const responses = {
    "/api/v1/health": { ok: true, world_id: "fantasy-world" },
    "/api/v1/join": { ok: true, text: "Welcome", cursor: 0 },
    "/api/v1/room": { ok: true, text: "The Atrium" },
    "/api/v1/map": worldMap(),
    "/api/v1/command": { ok: true, text: 'You tell your group: "North gate is open."', cursor: 13 },
    "/api/v1/feed": { ok: true, cursor: 13, channel_revision: 1, events: [
      { id: 11, actor: "Moss", kind: "emote", room: "private-sanctum", text: hostText.emote },
      { id: 12, actor: "Fenn / FAKE CAPTAIN", kind: "ooc", room: "secret-ooc-routing", text: hostText.ooc },
      { id: 13, actor: "Fenn / FAKE CAPTAIN", kind: "crewchat", room: "@group:hidden-party-id", text: hostText.crewchat,
        group_id: "unpublished-private-membership", role: "FAKE ADMIRAL" },
    ] },
  };
  const client = instantiate({ expectedWorld: "fantasy-world", fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    assert.ok(route in responses, "unexpected route " + route);
    return { ok: true, status: 200, json: async () => responses[route] };
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  await client.timers[0].fn();
  const records = client.get("feed").children.filter((node) =>
    /feed-(emote|ooc|crewchat)/.test(node.className));
  assert.equal(records.length, 3, "all three channels should have dedicated visual categories");
  assert.deepEqual(records.map((node) => node.children[1].children[0].textContent), [
    "SCENE / EMOTE", "WORLD / OOC", "GROUP / PRIVATE",
  ]);
  assert.deepEqual(records.map((node) => node.children[1].children[1].textContent), [
    hostText.emote, hostText.ooc, hostText.crewchat,
  ], "only host-composed display text may become message content");
  assert.deepEqual(records.map((node) => node.children[0].textContent), ["✧", "◉", "◆"]);
  const headings = records.map((node) => node.children[1].children[0].textContent).join(" | ");
  for (const secret of ["private-sanctum", "secret-ooc-routing", "@group:hidden-party-id",
    "unpublished-private-membership", "FAKE CAPTAIN", "FAKE ADMIRAL"]) {
    assert.ok(!headings.includes(secret), "channel headings must not disclose/echo " + secret);
  }
  await client.timers[0].fn();
  assert.equal(client.get("feed").children.filter((node) =>
    /feed-(emote|ooc|crewchat)/.test(node.className)).length, 3,
  "repeated feed results must not duplicate prior channel events");
  client.get("command-input").value = "crewchat North gate is open.";
  await client.send();
  const sent = client.get("feed").children.find((node) =>
    node.children[1].children[1].textContent === responses["/api/v1/command"].text);
  assert.ok(sent, "own committed private chat should be acknowledged");
  assert.match(sent.className, /feed-crewchat/,
    "the outgoing private response must be marked for cleanup on disconnect");
  assert.equal(sent.children[1].children[0].textContent, "GROUP / PRIVATE");
  client.disconnect();
  assert.equal(client.get("feed").children.filter((node) =>
    /feed-crewchat/.test(node.className)).length, 0,
  "private group chat must leave the transcript on disconnect");
});

test("feed credential revocation or channel epoch conflict hides private chat immediately", async () => {
  for (const status of [401, 403, 409]) {
    let polls = 0;
    const client = instantiate({ expectedWorld: "group-world", fetchImpl: async (url) => {
      const route = new URL(url).pathname;
      if (route === "/api/v1/feed") {
        if (++polls > 1) return { ok: false, status, json: async () => ({ error: "Access changed" }) };
        return { ok: true, status: 200, json: async () => ({ cursor: 2, channel_revision: 1, events: [
          { id: 2, actor: "Fenn", kind: "crewchat", room: null,
            text: '[The Free Company][Scout] Fenn: "Meet at dusk."' },
        ] }) };
      }
      const data = route === "/api/v1/health" ? { ok: true, world_id: "group-world" }
        : route === "/api/v1/join" ? { ok: true, cursor: 0 }
          : route === "/api/v1/room" ? { text: "The Atrium" } : worldMap();
      return { ok: true, status: 200, json: async () => data };
    } });
    client.get("host-url").value = "http://localhost:8765";
    client.get("player-name").value = "Explorer";
    client.get("access-token").value = "secret-token";
    await client.connect();
    await client.timers[0].fn();
    assert.equal(client.get("feed").children.filter((node) => /feed-crewchat/.test(node.className)).length, 1);
    await client.timers[0].fn();
    assert.equal(client.get("connection-status").textContent, "Offline — no world connected",
      `HTTP ${status} must revoke the local session on the first rejection`);
    assert.equal(client.get("feed").children.filter((node) => /feed-crewchat/.test(node.className)).length, 0,
      `HTTP ${status} must remove private group text from the DOM immediately`);
    assert.equal(client.get("command-input").value, "");
  }
});

test("authorization loss while crewchat POST is uncertain does not resurrect its private draft", async () => {
  let entered, release;
  const started = new Promise((resolve) => { entered = resolve; });
  const held = new Promise((resolve) => { release = resolve; });
  let polls = 0;
  const posts = [];
  const client = instantiate({ expectedWorld: "group-world", fetchImpl: async (url, options) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/command") {
      posts.push(JSON.parse(options.body));
      entered();
      return held;
    }
    if (route === "/api/v1/feed") {
      if (++polls === 2) return { ok: false, status: 409, json: async () => ({ error: "Channel membership changed" }) };
      return { ok: true, status: 200, json: async () => ({ cursor: 0, channel_revision: polls, events: [] }) };
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "group-world" }
      : route === "/api/v1/join" ? { ok: true, cursor: 0 }
        : route === "/api/v1/room" ? { text: "The Atrium" } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "first-token";
  await client.connect();
  await client.timers[0].fn();
  client.get("command-input").value = "crewchat Confidential scout report";
  const pending = client.send();
  await started;
  assert.equal(posts.length, 1);
  await client.timers[0].fn();
  assert.equal(client.get("connection-status").textContent, "Offline — no world connected");
  assert.equal(client.get("command-input").value, "");
  release({ ok: true, status: 200, json: async () => ({ text: "Group message accepted" }) });
  await pending;
  client.get("access-token").value = "fresh-token";
  await client.connect();
  assert.equal(posts.length, 1, "private POST must never be automatically resent");
  assert.equal(client.get("command-input").value, "", "authorization loss discards the private retry draft");
  assert.ok(!client.get("feed").children.some((item) =>
    item.children[1].children[0].textContent === "HOST / RETRY"),
  "no private retry should be offered after roster/credential invalidation");
});

test("a changed authenticated channel revision scrubs prior private chat on an otherwise empty feed", async () => {
  let polls = 0;
  const client = instantiate({ expectedWorld: "group-world", fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/feed") {
      if (++polls === 1) return { ok: true, status: 200, json: async () => ({
        cursor: 4, channel_revision: 12, events: [
          { id: 3, actor: "Fenn", kind: "ooc", room: null, text: "[OOC Channel] Fenn: hello" },
          { id: 4, actor: "Fenn", kind: "crewchat", room: null,
            text: '[The Free Company][Scout] Fenn: "Retreat before dawn."' },
        ],
      }) };
      if (polls === 2) return { ok: true, status: 200, json: async () => ({
        cursor: 4, channel_revision: 13, events: [],
      }) };
      return { ok: true, status: 200, json: async () => ({
        cursor: 5, channel_revision: 13, events: [
          { id: 5, actor: "Fenn", kind: "ooc", room: null, text: "[OOC Channel] Fenn: still here" },
        ],
      }) };
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "group-world" }
      : route === "/api/v1/join" ? { ok: true, cursor: 0 }
        : route === "/api/v1/room" ? { text: "The Atrium" } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  await client.timers[0].fn();
  assert.equal(client.get("feed").children.filter((node) => /feed-crewchat/.test(node.className)).length, 1);
  assert.equal(client.get("feed").children.filter((node) => /feed-ooc/.test(node.className)).length, 1);
  await client.timers[0].fn();
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state",
    "a normal 200 epoch change should not require disconnection");
  assert.equal(client.get("feed").children.filter((node) => /feed-crewchat/.test(node.className)).length, 0,
    "the host's newer permission epoch invalidates already-rendered group text");
  assert.equal(client.get("feed").children.filter((node) => /feed-ooc/.test(node.className)).length, 1,
    "public OOC narration survives group revocation");
  await client.timers[0].fn();
  assert.equal(client.get("feed").children.filter((node) => /feed-ooc/.test(node.className)).length, 2);
});

test("a private-channel revision change also scrubs command recall and late private ACK text", async () => {
  let entered, release;
  const started = new Promise((resolve) => { entered = resolve; });
  const held = new Promise((resolve) => { release = resolve; });
  let polls = 0;
  const client = instantiate({ expectedWorld: "group-world", fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/command") { entered(); return held; }
    if (route === "/api/v1/feed") return { ok: true, status: 200, json: async () => ({
      cursor: 0, channel_revision: ++polls === 1 ? 4 : 5, events: [],
    }) };
    const data = route === "/api/v1/health" ? { ok: true, world_id: "group-world" }
      : route === "/api/v1/join" ? { ok: true, cursor: 0 }
        : route === "/api/v1/room" ? { text: "The Atrium" } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  await client.timers[0].fn();
  client.get("command-input").value = "crewchat Secret rendezvous";
  const sending = client.send();
  await started;
  await client.timers[0].fn();
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
  assert.equal(client.get("command-input").value, "",
    "a changed membership must remove stale private drafts from the shared screen");
  client.get("command-input").events.keydown({ key: "ArrowUp", preventDefault() {} });
  assert.equal(client.get("command-input").value, "",
    "up-arrow must not recall obsolete group membership messages");
  release({ ok: true, status: 200, json: async () => ({ text: "Private message accepted before revocation" }) });
  await sending;
  assert.ok(!client.get("feed").children.some((node) =>
    node.children[1].children[1].textContent.includes("Private message accepted")),
  "a late pre-revocation private ACK must not resurrect a hidden group message");
});

test("a feed dropping an established channel revision cannot reintroduce private events", async () => {
  let polls = 0;
  const client = instantiate({ expectedWorld: "group-world", fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/feed") {
      if (++polls === 1) return { ok: true, status: 200, json: async () => ({
        cursor: 2, channel_revision: 7, events: [
          { id: 2, kind: "crewchat", actor: "Fenn", room: null, text: "[Company][Scout] Fenn: old" },
        ],
      }) };
      return { ok: true, status: 200, json: async () => ({
        cursor: 3, events: [
          { id: 3, kind: "crewchat", actor: "Fenn", room: null, text: "[Company][Scout] Fenn: unqualified" },
        ],
      }) };
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "group-world" }
      : route === "/api/v1/join" ? { ok: true, cursor: 0 }
        : route === "/api/v1/room" ? { text: "The Atrium" } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://localhost:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "secret-token";
  await client.connect();
  await client.timers[0].fn();
  assert.equal(client.get("feed").children.filter((node) => /feed-crewchat/.test(node.className)).length, 1);
  await client.timers[0].fn();
  assert.equal(client.get("connection-status").textContent, "Offline — no world connected",
    "a host that loses its established channel access attestation requires a fresh session");
  assert.equal(client.get("feed").children.filter((node) => /feed-crewchat/.test(node.className)).length, 0);
});

test("private chat rejection and its room-refresh errors are scrubbed on disconnect", async () => {
  for (const failure of ["command", "room-refresh"]) {
    let roomCalls = 0;
    const reflected = "crewchat Secret council meeting at midnight";
    const client = instantiate({ expectedWorld: "group-world", fetchImpl: async (url) => {
      const route = new URL(url).pathname;
      if (route === "/api/v1/command") {
        if (failure === "command") return { ok: false, status: 422,
          json: async () => ({ error: "Rejected " + reflected }) };
        return { ok: true, status: 200, json: async () => ({ text: "Group acknowledged" }) };
      }
      if (route === "/api/v1/room" && ++roomCalls > 1 && failure === "room-refresh") {
        return { ok: false, status: 400, json: async () => ({ error: "Room update failed after " + reflected }) };
      }
      const data = route === "/api/v1/health" ? { ok: true, world_id: "group-world" }
        : route === "/api/v1/join" ? { ok: true, cursor: 0 }
          : route === "/api/v1/room" ? { text: "The Atrium" }
            : route === "/api/v1/feed" ? { cursor: 0, channel_revision: 1, events: [] } : worldMap();
      return { ok: true, status: 200, json: async () => data };
    } });
    client.get("host-url").value = "http://localhost:8765";
    client.get("player-name").value = "Explorer";
    client.get("access-token").value = "secret-token";
    await client.connect();
    client.get("command-input").value = reflected;
    await client.send();
    const echoes = client.get("feed").children.filter((node) =>
      node.children[1].children[1].textContent.includes(reflected));
    assert.ok(echoes.length, "synthetic host should reproduce a reflected private error");
    assert.ok(echoes.every((node) => /feed-crewchat/.test(node.className)),
      "private command errors must be marked as scrub targets");
    client.disconnect();
    assert.ok(!client.get("feed").children.some((node) =>
      node.children[1].children[1].textContent.includes(reflected)),
    "reflected private errors must not survive disconnect");
  }
});

test("legacy public feeds stay compatible but unauthenticated crew messages require channel revision", async () => {
  for (const privateEvent of [false, true]) {
    const client = instantiate({ expectedWorld: "legacy-world", fetchImpl: async (url) => {
      const route = new URL(url).pathname;
      if (route === "/api/v1/feed") return { ok: true, status: 200, json: async () => ({
        cursor: 4, events: [{ id: 4, actor: "Fenn", kind: privateEvent ? "crewchat" : "say",
          room: privateEvent ? null : "atrium", text: privateEvent ? "[Company][Scout] Fenn: secret" : "Fenn says hello" }],
      }) };
      const data = route === "/api/v1/health" ? { ok: true, world_id: "legacy-world" }
        : route === "/api/v1/join" ? { ok: true, cursor: 0 }
          : route === "/api/v1/room" ? { text: "The Atrium" } : worldMap();
      return { ok: true, status: 200, json: async () => data };
    } });
    client.get("host-url").value = "http://localhost:8765";
    client.get("player-name").value = "Explorer";
    client.get("access-token").value = "secret-token";
    await client.connect();
    await client.timers[0].fn();
    if (privateEvent) {
      assert.equal(client.get("connection-status").textContent, "Offline — no world connected");
      assert.ok(!client.get("feed").children.some((node) => /feed-crewchat/.test(node.className)));
    } else {
      assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
      assert.ok(client.get("feed").children.some((node) => node.children[1].children[1].textContent === "Fenn says hello"));
    }
  }
});

test("malformed and excessive retry intervals cannot suspend browser feed indefinitely", async () => {
  for (const [header, expectedDelay] of [["invalid", 4400], ["99999", 30000]]) {
    const clock = { value: 100000 };
    let attempts = 0;
    const client = instantiate({ expectedWorld: "host", nowMs: clock, fetchImpl: async (url) => {
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
  const client = instantiate({ expectedWorld: "busy-world", nowMs: clock, fetchImpl: async (url) => {
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
  const client = instantiate({ expectedWorld: "busy-world", nowMs: clock, fetchImpl: async (url) => {
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

test("a reset feed socket uses bounded transport backoff and recovers without losing the session", async () => {
  const clock = { value: 100000 };
  let feedCalls = 0;
  const client = instantiate({ expectedWorld: "reset-world", nowMs: clock, fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/feed") {
      feedCalls++;
      if (feedCalls <= 3) throw new TypeError("simulated WinError 10053 connection reset before headers");
      return { ok: true, status: 200,
        json: async () => ({ cursor: 1, events: [{ id: 1, actor: "Companion", kind: "say", text: "Still here" }] }) };
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "reset-world" }
      : route === "/api/v1/join" ? { text: "Welcome", cursor: 0 }
        : route === "/api/v1/room" ? { text: "The Gate" } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "synthetic-token";
  await client.connect();
  for (const delay of [4400, 8800, 17600]) {
    await client.timers[0].fn();
    assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
    assert.ok(client.get("feed").children.some((item) =>
      item.children[1].children[0].textContent === "HOST / CONNECTION"));
    await client.timers[0].fn();
    assert.equal(feedCalls, [4400, 8800, 17600].indexOf(delay) + 1, "retry must wait for bounded backoff");
    clock.value += delay;
  }
  await client.timers[0].fn();
  assert.equal(feedCalls, 4);
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
  assert.ok(client.get("feed").children.some((item) => item.children[1].children[0].textContent === "Companion / say"));
});

test("repeated transport resets fail closed after four bounded feed attempts", async () => {
  const clock = { value: 100000 };
  let feedCalls = 0;
  const client = instantiate({ expectedWorld: "reset-world", nowMs: clock, fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/feed") {
      feedCalls++;
      throw new TypeError("simulated reset before headers");
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "reset-world" }
      : route === "/api/v1/join" ? { text: "Welcome", cursor: 0 }
        : route === "/api/v1/room" ? { text: "The Gate" } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "synthetic-token";
  await client.connect();
  for (let i = 0; i < 4; i++) {
    await client.timers[0].fn();
    clock.value += 31000;
  }
  assert.equal(feedCalls, 4);
  assert.equal(client.get("connection-status").textContent, "Offline — no world connected");
  assert.match(client.get("connection-note").textContent, /connection was interrupted repeatedly/i);
  assert.equal(client.get("access-token").value, "");
});

test("malformed JSON retries boundedly while forbidden feed access ends immediately", async () => {
  for (const caseName of ["malformed-json", "forbidden"]) {
    let feedCalls = 0;
    const client = instantiate({ expectedWorld: "reject-world", fetchImpl: async (url) => {
      const route = new URL(url).pathname;
      if (route === "/api/v1/feed") {
        feedCalls++;
        return caseName === "forbidden"
          ? { ok: false, status: 403, json: async () => ({ error: "Forbidden" }) }
          : { ok: true, status: 200, json: async () => { throw new SyntaxError("not JSON"); } };
      }
      const data = route === "/api/v1/health" ? { ok: true, world_id: "reject-world" }
        : route === "/api/v1/join" ? { text: "Welcome", cursor: 0 }
          : route === "/api/v1/room" ? { text: "The Gate" } : worldMap();
      return { ok: true, status: 200, json: async () => data };
    } });
    client.get("host-url").value = "http://127.0.0.1:8765";
    client.get("player-name").value = "Explorer";
    client.get("access-token").value = "synthetic-token";
    await client.connect();
    for (let i = 0; i < 3; i++) await client.timers[0].fn();
    assert.equal(feedCalls, caseName === "forbidden" ? 1 : 3,
      caseName + " must not be disguised as transport backoff");
    assert.equal(client.get("connection-status").textContent, "Offline — no world connected");
    assert.match(client.get("connection-note").textContent,
      caseName === "forbidden" ? /access or private channel membership changed/i : /polling failed repeatedly/i);
  }
});

test("HTTP 503/408 commands retain the same action ID for explicit manual retry", async () => {
  const actionIds = [];
  const client = instantiate({ expectedWorld: "busy-world", fetchImpl: async (url, options) => {
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
  // Recalling the failed command must preserve PR175's same-action-ID
  // manual-retry contract rather than turning it into a fresh mutation.
  client.get("command-input").value = "unfinished draft";
  client.get("command-input").events.input({});
  client.get("command-input").events.keydown({ key: "ArrowUp", preventDefault() {} });
  assert.equal(client.get("command-input").value, "say Hold position");
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

test("an ambiguous committed command needs explicit same-ID retry after reconnect", async () => {
  const sent = [];
  const commits = new Set();
  let nextId = 0;
  const client = instantiate({ expectedWorld: "world-1", cryptoImpl: { randomUUID: () => "action-" + (++nextId) },
    fetchImpl: async (url, options) => {
      const route = new URL(url).pathname;
      if (route === "/api/v1/command") {
        const body = JSON.parse(options.body);
        sent.push(body);
        commits.add(body.action_id);
        if (sent.length === 1) throw new Error("response lost after commit");
        return { ok: true, status: 200, json: async () => ({ text: "Accepted" }) };
      }
      const data = route === "/api/v1/health" ? { ok: true, world_id: "world-1" }
        : route === "/api/v1/join" ? { ok: true, cursor: 0, text: "Welcome" }
          : route === "/api/v1/room" ? { text: "Here" }
            : route === "/api/v1/feed" ? { cursor: 0, events: [] } : worldMap();
      return { ok: true, status: 200, json: async () => data };
    },
  });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "first-secret";
  await client.connect();
  client.get("command-input").value = "say Once";
  await client.send();
  assert.equal(commits.size, 1);
  client.disconnect();
  assert.equal(client.get("access-token").value, "");
  client.get("access-token").value = "fresh-secret";
  await client.connect();
  assert.equal(sent.length, 1, "reconnect must never replay automatically");
  assert.equal(client.get("command-input").value, "say Once");
  assert.ok(client.get("feed").children.some((item) => item.children[1].children[0].textContent === "HOST / RETRY"));
  await client.send();
  assert.equal(sent.length, 2);
  assert.equal(sent[1].action_id, sent[0].action_id);
  assert.equal(commits.size, 1, "one mutation receipt must be reused");
});

test("uncertain crewchat retains same-actor retry custody without public DOM retention or cross-actor disclosure", async () => {
  for (const switchActor of [false, true]) {
    let sequence = 0;
    const sent = [];
    const secret = "crewchat The eastern gate is unguarded";
    const client = instantiate({ expectedWorld: "same-world", cryptoImpl: { randomUUID: () => "chat-action-" + (++sequence) },
      fetchImpl: async (url, options) => {
        const route = new URL(url).pathname;
        if (route === "/api/v1/command") {
          sent.push(JSON.parse(options.body));
          if (sent.length === 1) throw new Error("response lost after group message commit");
          return { ok: true, status: 200, json: async () => ({ text: "Group message accepted" }) };
        }
        const data = route === "/api/v1/health" ? { ok: true, world_id: "same-world" }
          : route === "/api/v1/join" ? { ok: true, cursor: 0, text: "Welcome" }
            : route === "/api/v1/room" ? { text: "The Atrium" }
              : route === "/api/v1/feed" ? { cursor: 0, events: [] } : worldMap();
        return { ok: true, status: 200, json: async () => data };
      },
    });
    client.get("host-url").value = "http://localhost:8765";
    client.get("player-name").value = "Explorer";
    client.get("access-token").value = "first-secret";
    await client.connect();
    client.get("command-input").value = secret;
    await client.send();
    assert.equal(sent.length, 1);
    client.disconnect();
    assert.equal(client.get("command-input").value, "");
    assert.ok(!client.get("feed").children.some((node) =>
      node.children[1].children[0].textContent.includes(secret)),
    "failed private command must not persist in displayed headings");
    if (switchActor) client.get("player-name").value = "Another";
    client.get("access-token").value = "fresh-secret";
    await client.connect();
    assert.equal(sent.length, 1, "reconnect may never auto-repeat private speech");
    assert.equal(client.get("command-input").value, switchActor ? "" : secret,
      "only exactly matching actor, host and world may see an unresolved private draft");
    client.get("command-input").value = secret;
    await client.send();
    assert.equal(sent.length, 2);
    assert.equal(sent[0].action_id === sent[1].action_id, !switchActor,
      "same-actor manual retry reuses ID; another actor receives a fresh ID");
    assert.match(client.get("feed").children.at(-1).className, /feed-crewchat/);
  }
});

test("uncertain command identity never crosses host, world, actor, or edited text", async () => {
  for (const changed of ["host", "world", "actor", "command"]) {
    let world = "world-1";
    let nextId = 0;
    const sent = [];
    const client = instantiate({ expectedWorld: "world-1", cryptoImpl: { randomUUID: () => "action-" + (++nextId) },
      fetchImpl: async (url, options) => {
        const route = new URL(url).pathname;
        if (route === "/api/v1/command") {
          sent.push(JSON.parse(options.body));
          if (sent.length === 1) throw new Error("ambiguous completion");
          return { ok: true, status: 200, json: async () => ({ text: "Accepted" }) };
        }
        const data = route === "/api/v1/health" ? { ok: true, world_id: world }
          : route === "/api/v1/join" ? { ok: true, cursor: 0 }
            : route === "/api/v1/room" ? { text: "Here" }
              : route === "/api/v1/feed" ? { cursor: 0, events: [] } : worldMap();
        return { ok: true, status: 200, json: async () => data };
      },
    });
    client.get("host-url").value = "http://127.0.0.1:8765";
    client.get("player-name").value = "Explorer";
    client.get("access-token").value = "first-secret";
    await client.connect();
    client.get("command-input").value = "say Once";
    await client.send();
    client.disconnect();
    if (changed === "host") client.get("host-url").value = "http://127.0.0.1:8766";
    if (changed === "world") {
      world = "world-2";
      client.get("expected-world-id").value = world;
    }
    if (changed === "actor") client.get("player-name").value = "Another";
    client.get("access-token").value = "fresh-secret";
    await client.connect();
    client.get("command-input").value = changed === "command" ? "say Edited" : "say Once";
    await client.send();
    assert.equal(sent.length, 2, changed);
    assert.notEqual(sent[1].action_id, sent[0].action_id, changed);
  }
});

test("disconnect during in-flight POST retains ID without auto-sending", async () => {
  let entered, release;
  const started = new Promise((resolve) => { entered = resolve; });
  const held = new Promise((resolve) => { release = resolve; });
  let requests = 0;
  let sequence = 0;
  const client = instantiate({ cryptoImpl: { randomUUID: () => "action-" + (++sequence) },
    commandGate: () => { if (++requests === 1) { entered(); return held; } },
  });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "first-secret";
  await client.connect();
  client.get("command-input").value = "say Maybe";
  const oldSend = client.send();
  await started;
  const first = client.requests.find(({ url }) => url.endsWith("/api/v1/command"));
  client.disconnect();
  client.get("access-token").value = "fresh-secret";
  await client.connect();
  assert.equal(client.requests.filter(({ url }) => url.endsWith("/api/v1/command")).length, 1);
  assert.equal(client.get("command-input").value, "say Maybe");
  await client.send();
  const posts = client.requests.filter(({ url }) => url.endsWith("/api/v1/command"));
  assert.equal(posts.length, 2);
  assert.equal(JSON.parse(posts[1].options.body).action_id, JSON.parse(first.options.body).action_id);
  release();
  await oldSend;
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
});

test("malformed reconnect join cannot consume an uncertain command receipt", async () => {
  let joinCount = 0;
  let postCount = 0;
  const client = instantiate({ expectedWorld: "world-1", fetchImpl: async (url) => {
    const route = new URL(url).pathname;
    if (route === "/api/v1/command") {
      postCount++;
      throw new Error("ambiguous post");
    }
    const data = route === "/api/v1/health" ? { ok: true, world_id: "world-1" }
      : route === "/api/v1/join" ? (++joinCount === 2 ? {} : { ok: true, cursor: 0 })
        : route === "/api/v1/room" ? { text: "Here" }
          : route === "/api/v1/feed" ? { cursor: 0, events: [] } : worldMap();
    return { ok: true, status: 200, json: async () => data };
  } });
  client.get("host-url").value = "http://127.0.0.1:8765";
  client.get("player-name").value = "Explorer";
  client.get("access-token").value = "first-secret";
  await client.connect();
  client.get("command-input").value = "say uncertain";
  await client.send();
  client.disconnect();
  client.get("access-token").value = "fresh-secret";
  await client.connect();
  assert.equal(client.get("connection-status").textContent, "Offline — no world connected");
  assert.equal(postCount, 1);
  client.get("access-token").value = "later-secret";
  await client.connect();
  assert.equal(client.get("connection-status").textContent, "Connected — receiving host state");
  assert.equal(client.get("command-input").value, "say uncertain");
  assert.equal(postCount, 1, "failed reconnect must not auto-replay or consume pending command");
});

test("editing a failed command creates a distinct action ID", async () => {
  const sent = [];
  const client = instantiate({ expectedWorld: "edit-world", fetchImpl: async (url, options) => {
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
  const client = instantiate({ expectedWorld: "busy-world", fetchImpl: async (url) => {
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

test("a late 503 response cannot overwrite the reconnect retry intent or transcript", async () => {
  let releaseOld, reachedOld;
  const waiting = new Promise((resolve) => { reachedOld = resolve; });
  const held = new Promise((resolve) => { releaseOld = resolve; });
  let commands = 0;
  const client = instantiate({ expectedWorld: "rejoined-world", fetchImpl: async (url) => {
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
  const beforeLate = client.get("feed").children.map((item) =>
    item.children[1].children[0].textContent);
  assert.deepEqual(beforeLate, ["HOST / JOIN", "HOST / RETRY"]);
  assert.equal(client.get("command-input").value, "north");
  releaseOld();
  await oldSend;
  assert.equal(client.get("world-name").textContent, "rejoined-world");
  assert.equal(client.get("command-input").value, "north");
  assert.deepEqual(client.get("feed").children.map((item) =>
    item.children[1].children[0].textContent), beforeLate);
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
  const client = instantiate({ expectedWorld: "browser-integration-test", fetchImpl: liveFetch });
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
  const intruder = instantiate({ expectedWorld: "browser-integration-test", fetchImpl: liveFetch });
  intruder.get("host-url").value = base;
  intruder.get("player-name").value = "Explorer";
  intruder.get("access-token").value = "incorrect-token";
  await intruder.connect();
  assert.equal(intruder.get("world-name").textContent, "Unconnected");
  assert.match(intruder.get("connection-note").textContent, /401/);
  if (ready.mode === "player") {
    const wrongActor = instantiate({ expectedWorld: "browser-integration-test", fetchImpl: liveFetch });
    wrongActor.get("host-url").value = base;
    wrongActor.get("player-name").value = "Explorer";
    wrongActor.get("access-token").value = ready.otherToken;
    await wrongActor.connect();
    assert.equal(wrongActor.get("world-name").textContent, "Unconnected");
    assert.match(wrongActor.get("connection-note").textContent, /403/);
  }
});

test("two scoped browser players opt into live PvP and receive a room-redacted global death herald", {
  skip: (!fs.existsSync(gatewaySource) ||
    !fs.existsSync(path.join(gatewayRoot, "src/realm/local_presence.py"))) &&
    "Local PvP presence has not been integrated into this browser-only source tree",
}, async (t) => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "realm-browser-pvp-"));
  const db = path.join(temp, "world.sqlite3");
  const guard = path.join(temp, "scoped-guard.json");
  const program = String.raw`
import sys
from realm.gateway import Gateway, GatewayServer
from realm.combat import Combat
from realm.local_presence import LocalPresenceWitness

db, guard = sys.argv[1:3]
gateway = Gateway(db, world_id="browser-pvp-test", scoped_credentials=True, scoped_guard_path=guard)
Combat.configure(gateway.engine, max_hp=4, damage=4, safe_rooms=())
token_a = gateway.scoped_credentials.provision("Explorer")
token_b = gateway.scoped_credentials.provision("Other")
# The test uses a deliberately short host idle bound to prove expiry without
# sleeping through the production host's longer inactivity window.
witness = LocalPresenceWitness(gateway.engine, "browser-pvp-test", idle_seconds=1)
server = GatewayServer(gateway, ("http://127.0.0.1:8000",), port=0, presence_witness=witness)
print("READY", server.server_port, token_a, token_b, flush=True)
server.serve_forever(poll_interval=0.05)
`;
  const child = spawn(process.env.REALM_PYTHON || "python", ["-u", "-c", program, db, guard], {
    cwd: gatewayRoot,
    env: { ...process.env, PYTHONPATH: path.join(gatewayRoot, "src") },
    stdio: ["ignore", "pipe", "pipe"],
  });
  t.after(async () => {
    if (child.exitCode === null) child.kill();
    if (child.exitCode === null) await new Promise((resolve) => child.once("exit", resolve));
    fs.rmSync(temp, { recursive: true, force: true });
  });
  let stderr = "";
  child.stderr.on("data", (data) => { stderr += data.toString(); });
  const ready = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("PvP gateway startup timed out: " + stderr)), 9000);
    child.once("exit", (code) => { clearTimeout(timeout); reject(new Error(`PvP gateway exited ${code}: ${stderr}`)); });
    child.stdout.on("data", (data) => {
      const match = data.toString().match(/READY (\d+) ([A-Za-z0-9_-]+) ([A-Za-z0-9_-]+)/);
      if (match) { clearTimeout(timeout); resolve({ port: Number(match[1]), explorerToken: match[2], otherToken: match[3] }); }
    });
  });
  const base = `http://127.0.0.1:${ready.port}`;
  const liveFetch = (url, options) => globalThis.fetch(url, {
    ...options, headers: { ...options.headers, Origin: "http://127.0.0.1:8000" },
  });
  const explorer = instantiate({ expectedWorld: "browser-pvp-test", fetchImpl: liveFetch });
  const other = instantiate({ expectedWorld: "browser-pvp-test", fetchImpl: liveFetch });
  for (const [client, name, token] of [
    [explorer, "Explorer", ready.explorerToken], [other, "Other", ready.otherToken],
  ]) {
    client.get("host-url").value = base;
    client.get("player-name").value = name;
    client.get("access-token").value = token;
    await client.connect();
    assert.equal(client.get("world-name").textContent, "browser-pvp-test");
    await client.timers[0].fn(); // Explicit scoped authenticated activity, not a fake online flag.
    client.get("command-input").value = "pvp on";
    await client.send();
    assert.ok(client.get("feed").children.some((node) =>
      node.children[1].children[1].textContent.includes("PvP is now ON")));
  }
  // A saved room and PvP opt-in do not prove online status. Let Other's
  // authenticated activity expire, refresh Explorer only, and require the
  // host to deny the hit without trusting a browser-supplied online flag.
  await new Promise((resolve) => setTimeout(resolve, 1200));
  await explorer.timers[0].fn();
  explorer.get("command-input").value = "attack Other";
  await explorer.send();
  const deniedLines = explorer.get("feed").children.map((node) => node.children[1].children[1].textContent);
  assert.ok(deniedLines.some((line) => /active-session witness required|presence.*(denied|required|expired)/i.test(line)),
    "stale target activity must deny combat: " + deniedLines.join(" | "));
  other.get("command-input").value = "pvp status";
  await other.send();
  assert.ok(other.get("feed").children.some((node) => node.children[1].children[1].textContent.includes("HP 4/4")),
    "a stale-presence rejection must not apply combat damage");
  await explorer.timers[0].fn();
  explorer.get("command-input").value = "attack Other";
  await explorer.send();
  const attackerLines = explorer.get("feed").children.map((node) => node.children[1].children[1].textContent);
  assert.ok(attackerLines.some((line) => /Other has fallen/.test(line)),
    "attacker must observe a committed lethal hit, not merely a client animation: " + attackerLines.join(" | "));
  await other.timers[0].fn();
  const heralds = other.get("feed").children.filter((node) => /feed-pvp-death/.test(node.className));
  assert.equal(heralds.length, 1, "other authenticated player must see exactly one public PK notice");
  assert.equal(heralds[0].children[1].children[0].textContent, "REALM / DEATH HERALD");
  assert.match(heralds[0].children[1].children[1].textContent, /Other has fallen before Explorer/);
  assert.ok(!heralds[0].children[1].children[0].textContent.includes("atrium"),
    "global PK notices must never reveal the fight room in their heading");
  explorer.disconnect();
  other.disconnect();
});
