"use strict";

/* Local, review-only REALM Creator. No source or credential persistence. */
(() => {
  const get = (id) => document.getElementById(id);
  const ui = {
    form: get("preview-form"),
    worldId: get("world-id"),
    worldName: get("world-name"),
    token: get("host-token"),
    sourceFiles: get("source-files"),
    preview: get("preview-button"),
    review: get("review-region"),
    rooms: get("room-editor"),
    ack: get("review-ack"),
    compile: get("compile-button"),
    status: get("status"),
    download: get("download-pack"),
  };
  const FILE_LIMIT = 8;
  const EACH_BYTES = 256 * 1024;
  const TOTAL_BYTES = 1024 * 1024;
  const NAME = /^[A-Za-z0-9][A-Za-z0-9 _.-]{0,99}\.(?:txt|md|markdown)$/;
  const RESERVED = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i;

  let current = null; // Only retained in this tab's memory, never persisted.
  let editors = [];
  let currentDownloadUrl = null;
  let formRevision = 0;
  let reviewRevision = 0;

  function setStatus(message) {
    ui.status.textContent = message;
  }

  function clearDownload() {
    if (currentDownloadUrl) URL.revokeObjectURL(currentDownloadUrl);
    currentDownloadUrl = null;
    ui.download.hidden = true;
    ui.download.href = "";
    ui.download.download = "";
  }

  function invalidateReviewedEdits() {
    reviewRevision += 1;
    ui.ack.checked = false;
    ui.compile.disabled = true;
    clearDownload();
  }

  function previewMatchesForm(snapshot) {
    if (!snapshot || snapshot !== current || snapshot.formRevision !== formRevision) return false;
    if (ui.worldId.value.trim() !== snapshot.request.world_id ||
        ui.worldName.value.trim() !== snapshot.request.name) return false;
    const files = Array.from(ui.sourceFiles.files || []);
    return files.length === snapshot.fileRefs.length &&
      files.every((file, i) => file === snapshot.fileRefs[i]);
  }

  function clearPreview() {
    current = null;
    editors = [];
    ui.rooms.replaceChildren();
    ui.review.hidden = true;
    ui.ack.checked = false;
    ui.compile.disabled = true;
    clearDownload();
  }

  function assertToken() {
    const token = ui.token.value.trim();
    if (!token) throw new Error("Enter the ephemeral host token from the Creator server console.");
    return token;
  }

  function bytesToBase64(bytes) {
    let binary = "";
    // Avoid spreading a large Uint8Array through the function argument stack.
    for (let offset = 0; offset < bytes.length; offset += 32768) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
    }
    return btoa(binary);
  }

  async function prepareUpload() {
    const files = Array.from(ui.sourceFiles.files || []);
    if (files.length < 1 || files.length > FILE_LIMIT) {
      throw new Error("Choose 1–8 UTF-8 .txt, .md or .markdown files.");
    }
    const seen = new Set();
    let total = 0;
    const sources = [];
    for (const file of files) {
      const name = file.name;
      if (typeof name !== "string" || !NAME.test(name) || RESERVED.test(name)) {
        throw new Error("File name must be a safe .txt/.md/.markdown basename.");
      }
      const folded = name.toLowerCase();
      if (seen.has(folded)) throw new Error("Duplicate file name (ignoring case): " + name);
      seen.add(folded);
      if (file.size < 1 || file.size > EACH_BYTES) {
        throw new Error("Every source file must be nonempty and at most 256 KiB.");
      }
      total += file.size;
      if (total > TOTAL_BYTES) throw new Error("Combined upload may not exceed 1 MiB.");
      const bytes = new Uint8Array(await file.arrayBuffer());
      if (bytes.length !== file.size) throw new Error("File changed during reading; retry.");
      try {
        new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      } catch {
        throw new Error("Sources must contain valid UTF-8 text.");
      }
      sources.push({ name, content_base64: bytesToBase64(bytes) });
    }
    return {
      world_id: ui.worldId.value.trim(),
      name: ui.worldName.value.trim(),
      sources,
    };
  }

  async function send(path, body) {
    const token = assertToken();
    const response = await fetch(path, {
      method: "POST",
      mode: "same-origin",
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      headers: {
        "Content-Type": "application/json",
        "X-REALM-CREATOR-TOKEN": token,
      },
      body: JSON.stringify(body),
    });
    const output = await response.json();
    if (!response.ok) {
      throw new Error(typeof output.error === "string" ? output.error :
        "Creator server rejected the request (HTTP " + response.status + ").");
    }
    return output;
  }

  function element(tag, parent, content) {
    const node = document.createElement(tag);
    if (content !== undefined) node.textContent = content;
    parent.appendChild(node);
    return node;
  }

  function renderReview(data) {
    if (!data || !data.draft || !data.region ||
        data.region.status !== "REVIEW_REQUIRED" ||
        data.region.canon_status !== "UNREVIEWED" ||
        !Array.isArray(data.region.rooms) || !data.region.rooms.length) {
      throw new Error("Server response is not a review-required region proposal.");
    }
    editors = [];
    ui.rooms.replaceChildren();
    for (const room of data.region.rooms) {
      const card = element("article", ui.rooms);
      card.className = "room-card";
      element("h3", card, room.name);
      element("p", card, "UNREVIEWED source heading. Proposed room, not canonical geography.");
      const evidence = room.name_evidence || {};
      element("p", card, "Source: " + String(evidence.source_sha256 || "?").slice(0, 16) +
        " · line " + String(evidence.start_line || "?"));
      element("pre", card, room.source_excerpt && room.source_excerpt.text || "");
      const nameLabel = element("label", card, "Reviewed room name");
      const nameInput = element("input", nameLabel);
      nameInput.value = room.name;
      nameInput.maxLength = 64;
      nameInput.addEventListener("input", invalidateReviewedEdits);
      const descLabel = element("label", card, "Creator-authored scene description");
      const description = element("textarea", descLabel);
      description.value = "";
      description.placeholder = room.description && room.description.text ||
        "Write the scene that players should see.";
      description.maxLength = 2000;
      description.addEventListener("input", invalidateReviewedEdits);
      const edgeBoxes = [];
      const exits = room.exits || {};
      for (const direction of Object.keys(exits)) {
        const label = element("label", card);
        const checkbox = element("input", label);
        checkbox.type = "checkbox";
        checkbox.checked = false; // Invented routes require affirmative opt-in.
        checkbox.addEventListener("change", invalidateReviewedEdits);
        element("span", label, "Add PROPOSED " + direction + " exit to " + exits[direction].to);
        edgeBoxes.push({ direction, destination: exits[direction].to, checkbox });
      }
      editors.push({ room, nameInput, description, edgeBoxes });
    }
    ui.ack.checked = false;
    ui.compile.disabled = true;
    ui.review.hidden = false;
    setStatus("Review required: source headings are unreviewed, and all suggested exits are invented gameplay proposals.");
  }

  function onFormChanged() {
    formRevision += 1;
    const hadPreview = Boolean(current);
    clearPreview();
    if (hadPreview) {
      setStatus("Source or identity changed. Generate a NEW preview before review or compilation.");
    }
  }

  for (const field of [ui.worldId, ui.worldName, ui.token]) {
    field.addEventListener("input", onFormChanged);
    field.addEventListener("change", onFormChanged);
  }
  ui.sourceFiles.addEventListener("change", onFormChanged);

  ui.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearPreview();
    const startedRevision = formRevision;
    const fileRefs = Array.from(ui.sourceFiles.files || []);
    try {
      // Validate the token before spending time reading any private file bytes.
      assertToken();
      const request = await prepareUpload();
      if (formRevision !== startedRevision ||
          ui.worldId.value.trim() !== request.world_id ||
          ui.worldName.value.trim() !== request.name ||
          Array.from(ui.sourceFiles.files || []).length !== fileRefs.length ||
          Array.from(ui.sourceFiles.files || []).some((file, i) => file !== fileRefs[i])) {
        setStatus("Upload inputs changed during reading. Generate a NEW preview.");
        return;
      }
      const output = await send("/api/v1/creator/preview", request);
      if (formRevision !== startedRevision) {
        setStatus("Preview inputs changed while waiting for the server. Generate a NEW preview.");
        return;
      }
      current = { request, draft: output.draft, region: output.region, fileRefs, formRevision: startedRevision };
      renderReview(output);
    } catch (error) {
      if (formRevision === startedRevision) {
        clearPreview();
        setStatus("Preview failed: " + error.message);
      }
    }
  });

  ui.ack.addEventListener("change", () => {
    reviewRevision += 1;
    if (!ui.ack.checked) clearDownload();
    ui.compile.disabled = !previewMatchesForm(current) || !ui.ack.checked;
  });

  ui.compile.addEventListener("click", async (event) => {
    event.preventDefault();
    const snapshot = current;
    if (!previewMatchesForm(snapshot)) {
      clearPreview();
      setStatus("Current sources or world identity differ from the reviewed preview. Generate a NEW preview.");
      return;
    }
    if (!ui.ack.checked) {
      setStatus("Check the explicit creator review acknowledgement before compiling.");
      return;
    }
    const approvedRevision = reviewRevision;
    clearDownload();
    try {
      const rooms = editors.map(({ room, nameInput, description, edgeBoxes }) => {
        const name = nameInput.value.trim();
        const scene = description.value.trim();
        if (!name || !scene) throw new Error("Author a nonempty reviewed name and description for every room.");
        const exits = {};
        for (const edge of edgeBoxes) {
          if (edge.checkbox.checked) exits[edge.direction] = edge.destination;
        }
        return {
          id: room.id, name, description: scene, exits,
          source_proposal_ids: [room.name_evidence.proposal_id],
        };
      });
      const approval = {
        schema: "realm.creator.approval.v1",
        draft_sha256: snapshot.draft.sha256,
        world_id: snapshot.draft.world_id,
        name: snapshot.draft.name,
        start_room: snapshot.region.start_room_candidate.id,
        rooms,
        items: [],
        acknowledged_review: true,
      };
      const output = await send("/api/v1/creator/compile", {
        ...snapshot.request,
        draft: snapshot.draft,
        approval,
      });
      if (!previewMatchesForm(snapshot) || reviewRevision !== approvedRevision || !ui.ack.checked) {
        clearDownload();
        setStatus("Review inputs changed during compilation. Review again and recompile.");
        return;
      }
      if (!output || !output.compiled || typeof output.worldpack_json !== "string" ||
          output.compiled.approval_authenticated !== false ||
          output.compiled.world_imported !== false) {
        throw new Error("Server did not return an explicitly unauthenticated, unimported worldpack.");
      }
      // Preserve the byte-for-byte canonical JSON supplied by the server. Do
      // not reserialize or pretty-print: this is the importer's hashed payload.
      const blob = new Blob([output.worldpack_json], { type: "application/json" });
      currentDownloadUrl = URL.createObjectURL(blob);
      ui.download.href = currentDownloadUrl;
      ui.download.download = snapshot.request.world_id + ".worldpack.json";
      ui.download.textContent = "Download unimported worldpack JSON";
      ui.download.hidden = false;
      setStatus("Compiled for download only. Approval NOT authenticated; worldpack NOT imported, deployed or canon.");
    } catch (error) {
      setStatus("Compile failed: " + error.message);
    }
  });

  if (window && window.addEventListener) {
    window.addEventListener("pagehide", clearDownload);
  }
  clearPreview();
  setStatus("Local Creator ready. Enter the ephemeral host token, select UTF-8 sources, then preview.");
})();
