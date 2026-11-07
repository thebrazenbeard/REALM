"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = new Map();
    this.textContent = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.files = [];
    this.href = "";
    this.download = "";
  }
  set innerHTML(_) { throw new Error("HTML injection sink must never be used"); }
  addEventListener(kind, fn) { this.listeners.set(kind, fn); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  async fire(kind) {
    const handler = this.listeners.get(kind);
    if (!handler) throw new Error("Missing event listener: " + kind);
    await handler({ preventDefault() {} });
  }
}

function findAll(node, tag) {
  return [...(node.tagName === tag.toUpperCase() ? [node] : []),
    ...node.children.flatMap(child => findAll(child, tag))];
}

function makeFile(name, text) {
  const bytes = Buffer.from(text, "utf8");
  return { name, size: bytes.length, async arrayBuffer() {
    return Uint8Array.from(bytes).buffer;
  } };
}

function createApp(fetchImpl) {
  const ids = ["world-id", "world-name", "host-token", "source-files", "preview-form",
    "preview-button", "review-region", "room-editor", "review-ack", "compile-button",
    "status", "download-pack"];
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  const blobs = [];
  const createdUrls = [];
  const storage = new Proxy({}, { get() { throw new Error("Credential storage forbidden"); } });
  const document = {
    getElementById(id) { if (!elements[id]) throw new Error("Unknown id " + id); return elements[id]; },
    createElement(tag) { return new Element(tag); },
  };
  const sandbox = {
    document, fetch: fetchImpl, TextDecoder, Uint8Array, Blob,
    btoa(s) { return Buffer.from(s, "binary").toString("base64"); },
    URL: {
      createObjectURL(blob) { blobs.push(blob); const url = "blob:review-" + blobs.length; createdUrls.push(url); return url; },
      revokeObjectURL() {},
    },
    localStorage: storage,
    sessionStorage: storage,
    window: { addEventListener() {} },
    console: { error() { throw new Error("Unexpected console logging"); } },
  };
  vm.runInNewContext(script, sandbox, { filename: "app.js" });
  elements["world-id"].value = "synthetic_region";
  elements["world-name"].value = "Synthetic Region";
  elements["host-token"].value = "ephemeral-secret";
  elements["source-files"].files = [makeFile("notes.md", "## Location: Copper Harbor\n<script>alert(1)</script>\n")];
  return { elements, blobs, createdUrls };
}

function samplePreview() {
  return {
    draft: { schema: "realm.creator.draft.v1", world_id: "synthetic_region",
      name: "Synthetic Region", sha256: "a".repeat(64) },
    region: { world_id: "synthetic_region", name: "Synthetic Region",
      status: "REVIEW_REQUIRED", canon_status: "UNREVIEWED",
      rooms: [{ id: "copper_harbor", name: "Copper Harbor",
        source_excerpt: { text: "<script>alert(1)</script>" },
        name_evidence: { proposal_id: "src-1234567890abcdef", source_sha256: "b".repeat(64), start_line: 1, end_line: 2 },
        description: { kind: "GAMEPLAY_PROPOSAL", text: "Unreviewed placeholder" },
        exits: { east: { to: "other_room", kind: "GAMEPLAY_PROPOSAL" } },
      }],
      start_room_candidate: { id: "copper_harbor", kind: "GAMEPLAY_PROPOSAL" },
    },
  };
}

test("preview sends bounded UTF-8 source and ephemeral token, renders untrusted text inertly", async () => {
  const requests = [];
  const { elements } = createApp(async (url, options) => {
    requests.push({ url, options });
    return { ok: true, async json() { return samplePreview(); } };
  });
  await elements["preview-form"].fire("submit");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/v1/creator/preview");
  assert.equal(requests[0].options.headers["X-REALM-CREATOR-TOKEN"], "ephemeral-secret");
  assert.equal(requests[0].options.credentials, "omit");
  const data = JSON.parse(requests[0].options.body);
  assert.equal(data.world_id, "synthetic_region");
  assert.equal(data.sources[0].name, "notes.md");
  assert.match(Buffer.from(data.sources[0].content_base64, "base64").toString("utf8"), /<script>alert/);
  const pre = findAll(elements["room-editor"], "pre");
  assert.equal(pre.length, 1);
  assert.equal(pre[0].textContent, "<script>alert(1)</script>");
  assert.equal(elements["compile-button"].disabled, true);
  assert.equal(elements["review-region"].hidden, false);
  assert.match(elements.status.textContent, /review/i);
});

test("compile needs explicit review and emits exact server worldpack bytes without importing", async () => {
  const requests = [];
  const exactJson = '{"schema":"realm.worldpack.v1","world_id":"synthetic_region"}';
  const { elements, blobs } = createApp(async (url, options) => {
    requests.push({ url, options });
    if (url.endsWith("/preview")) return { ok: true, async json() { return samplePreview(); } };
    return { ok: true, async json() {
      return { compiled: { approval_authenticated: false, world_imported: false },
        worldpack_json: exactJson };
    } };
  });
  await elements["preview-form"].fire("submit");
  assert.equal(requests.length, 1);
  await elements["compile-button"].fire("click");
  assert.equal(requests.length, 1);
  const textareas = findAll(elements["room-editor"], "textarea");
  assert.equal(textareas.length, 1);
  textareas[0].value = "A creator-reviewed harbor.";
  elements["review-ack"].checked = true;
  await elements["review-ack"].fire("change");
  assert.equal(elements["compile-button"].disabled, false);
  await elements["compile-button"].fire("click");
  assert.equal(requests.length, 2);
  assert.equal(requests[1].url, "/api/v1/creator/compile");
  const payload = JSON.parse(requests[1].options.body);
  assert.equal(payload.approval.schema, "realm.creator.approval.v1");
  assert.equal(payload.approval.draft_sha256, "a".repeat(64));
  assert.deepEqual(Array.from(payload.approval.rooms[0].source_proposal_ids), ["src-1234567890abcdef"]);
  assert.equal(payload.approval.rooms[0].description, "A creator-reviewed harbor.");
  assert.deepEqual(Object.keys(payload.approval.rooms[0].exits), []);
  assert.equal(payload.approval.acknowledged_review, true);
  assert.equal(blobs.length, 1);
  assert.equal(await blobs[0].text(), exactJson);
  assert.equal(elements["download-pack"].hidden, false);
  assert.equal(elements["download-pack"].download, "synthetic_region.worldpack.json");
  assert.match(elements.status.textContent, /not imported/i);
});

test("invalid upload and absent token fail locally without fetch or token persistence", async () => {
  const requests = [];
  const { elements } = createApp(async () => { requests.push(true); throw new Error("must not fetch"); });
  elements["host-token"].value = "";
  await elements["preview-form"].fire("submit");
  assert.equal(requests.length, 0);
  assert.match(elements.status.textContent, /token/i);
  elements["host-token"].value = "host-token";
  elements["source-files"].files = [makeFile("../evil.md", "content")];
  await elements["preview-form"].fire("submit");
  assert.equal(requests.length, 0);
  assert.match(elements.status.textContent, /file|name|basename/i);
  elements["source-files"].files = [makeFile("notes.md", "")];
  await elements["preview-form"].fire("submit");
  assert.equal(requests.length, 0);
  assert.match(elements.status.textContent, /empty|size|file/i);
});


test("HTML declares every wired input and uses only external script/styles", () => {
  const html = fs.readFileSync(path.join(__dirname, "static", "index.html"), "utf8");
  for (const id of ["preview-form", "world-id", "world-name", "host-token", "source-files",
    "review-region", "room-editor", "review-ack", "compile-button", "status", "download-pack"]) {
    assert.match(html, new RegExp('id="' + id + '"'));
  }
  assert.match(html, /<script src="\/app\.js" defer><\/script>/);
  assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)/);
  assert.doesNotMatch(html, /\son[a-z]+\s*=/i);
  assert.match(html, /No automatic canon/);
});

test("source limits and server errors fail closed without exposing a reviewed pack", async () => {
  const requests = [];
  const { elements } = createApp(async (url, options) => {
    requests.push({ url, options });
    return { ok: false, status: 413, async json() { return { error: "Upload too large" }; } };
  });
  elements["source-files"].files = [Object.assign(makeFile("notes.md", "test"), { size: 256 * 1024 + 1 })];
  await elements["preview-form"].fire("submit");
  assert.equal(requests.length, 0);
  assert.match(elements.status.textContent, /256 KiB/i);
  elements["source-files"].files = [makeFile("notes.md", "valid")];
  await elements["preview-form"].fire("submit");
  assert.equal(requests.length, 1);
  assert.match(elements.status.textContent, /Upload too large/);
  assert.equal(elements["review-region"].hidden, true);
  assert.equal(elements["download-pack"].hidden, true);
});

test("a proposed exit is included only after a separate positive choice", async () => {
  const requests = [];
  const { elements } = createApp(async (url, options) => {
    requests.push({ url, options });
    if (url.endsWith("/preview")) return { ok: true, async json() { return samplePreview(); } };
    return { ok: true, async json() { return {
      compiled: { approval_authenticated: false, world_imported: false },
      worldpack_json: '{"schema":"realm.worldpack.v1"}',
    }; } };
  });
  await elements["preview-form"].fire("submit");
  const description = findAll(elements["room-editor"], "textarea")[0];
  description.value = "A reviewed harbor with a copper quay.";
  const proposedEdge = findAll(elements["room-editor"], "input").find(node => node.type === "checkbox");
  assert.ok(proposedEdge);
  proposedEdge.checked = true;
  elements["review-ack"].checked = true;
  await elements["review-ack"].fire("change");
  await elements["compile-button"].fire("click");
  assert.equal(requests.length, 2);
  const payload = JSON.parse(requests[1].options.body);
  assert.equal(payload.approval.rooms[0].exits.east, "other_room");
});


test("changing source, world identity, or host token invalidates the reviewed preview", async () => {
  const cases = [
    { field: "world-id", kind: "input", replace(el) { el.value = "a_different_world"; } },
    { field: "world-name", kind: "input", replace(el) { el.value = "A Different Name"; } },
    { field: "host-token", kind: "input", replace(el) { el.value = "a-new-token"; } },
    { field: "source-files", kind: "change", replace(el) { el.files = [makeFile("updated.md", "## Location: New Shore")]; } },
  ];
  for (const scenario of cases) {
    const requests = [];
    const { elements } = createApp(async (url, options) => {
      requests.push({ url, options });
      if (url.endsWith("/preview")) return { ok: true, async json() { return samplePreview(); } };
      throw new Error("Must not compile stale source data");
    });
    await elements["preview-form"].fire("submit");
    assert.equal(requests.length, 1);
    elements["review-ack"].checked = true;
    await elements["review-ack"].fire("change");
    assert.equal(elements["compile-button"].disabled, false);
    scenario.replace(elements[scenario.field]);
    await elements[scenario.field].fire(scenario.kind);
    assert.equal(elements["review-region"].hidden, true, scenario.field);
    assert.equal(elements["compile-button"].disabled, true, scenario.field);
    assert.equal(elements["review-ack"].checked, false, scenario.field);
    assert.equal(elements["download-pack"].hidden, true, scenario.field);
    await elements["compile-button"].fire("click");
    assert.equal(requests.length, 1, scenario.field);
    assert.match(elements.status.textContent, /preview|review/i, scenario.field);
  }
});

test("editing a room after review acknowledgement requires renewed acknowledgement", async () => {
  const requests = [];
  const { elements } = createApp(async (url, options) => {
    requests.push({ url, options });
    if (url.endsWith("/preview")) return { ok: true, async json() { return samplePreview(); } };
    throw new Error("Should not compile after unreviewed edit");
  });
  await elements["preview-form"].fire("submit");
  elements["review-ack"].checked = true;
  await elements["review-ack"].fire("change");
  const textArea = findAll(elements["room-editor"], "textarea")[0];
  textArea.value = "Changed after approval";
  await textArea.fire("input");
  assert.equal(elements["review-ack"].checked, false);
  assert.equal(elements["compile-button"].disabled, true);
  assert.equal(elements["review-region"].hidden, false);
  await elements["compile-button"].fire("click");
  assert.equal(requests.length, 1);
});


test("delayed preview response cannot restore a draft after visible inputs change", async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const calls = [];
  const { elements } = createApp(async (url, options) => {
    calls.push({ url, options });
    return await gate;
  });
  const pendingPreview = elements["preview-form"].fire("submit");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 1);
  elements["world-name"].value = "Changed while waiting";
  await elements["world-name"].fire("input");
  release({ ok: true, async json() { return samplePreview(); } });
  await pendingPreview;
  assert.equal(elements["review-region"].hidden, true);
  assert.equal(elements["compile-button"].disabled, true);
  assert.equal(elements["review-ack"].checked, false);
  assert.match(elements.status.textContent, /NEW preview/i);
});

test("delayed compile response is discarded after source or reviewed scene changes", async () => {
  for (const changed of ["source", "scene"]) {
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    const calls = [];
    const { elements, blobs } = createApp(async (url, options) => {
      calls.push({ url, options });
      if (url.endsWith("/preview")) return { ok: true, async json() { return samplePreview(); } };
      return await gate;
    });
    await elements["preview-form"].fire("submit");
    const description = findAll(elements["room-editor"], "textarea")[0];
    description.value = "The reviewed copper quay.";
    elements["review-ack"].checked = true;
    await elements["review-ack"].fire("change");
    const pendingCompile = elements["compile-button"].fire("click");
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls.length, 2);
    if (changed === "source") {
      elements["source-files"].files = [makeFile("new.md", "## Location: Elsewhere")];
      await elements["source-files"].fire("change");
    } else {
      description.value = "Changed after compile started.";
      await description.fire("input");
    }
    release({ ok: true, async json() {
      return { compiled: { approval_authenticated: false, world_imported: false },
        worldpack_json: '{"schema":"realm.worldpack.v1"}' };
    } });
    await pendingCompile;
    assert.equal(blobs.length, 0, changed);
    assert.equal(elements["download-pack"].hidden, true, changed);
    assert.equal(elements["compile-button"].disabled, true, changed);
    assert.match(elements.status.textContent, /changed|review/i, changed);
  }
});


test("changing reviewed room name or proposed route revokes prior acknowledgement", async () => {
  for (const kind of ["name", "exit"]) {
    const { elements } = createApp(async () => ({ ok: true, async json() { return samplePreview(); } }));
    await elements["preview-form"].fire("submit");
    elements["review-ack"].checked = true;
    await elements["review-ack"].fire("change");
    const inputs = findAll(elements["room-editor"], "input");
    const target = kind === "name" ? inputs.find(node => node.type !== "checkbox") :
      inputs.find(node => node.type === "checkbox");
    assert.ok(target);
    if (kind === "name") target.value = "Changed after review";
    else target.checked = true;
    await target.fire(kind === "name" ? "input" : "change");
    assert.equal(elements["review-ack"].checked, false, kind);
    assert.equal(elements["compile-button"].disabled, true, kind);
  }
});
