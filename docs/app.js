/* REALM's static browser client. No library, tracking, or token persistence. */
(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const ui = {
    connectionForm: byId("connection-form"),
    commandForm: byId("command-form"),
    hostUrl: byId("host-url"),
    playerName: byId("player-name"),
    accessToken: byId("access-token"),
    connect: byId("connect-button"),
    disconnect: byId("disconnect-button"),
    note: byId("connection-note"),
    dot: byId("connection-dot"),
    connectionStatus: byId("connection-status"),
    mode: byId("mode-label"),
    worldName: byId("world-name"),
    player: byId("current-player"),
    worldState: byId("world-state"),
    accessState: byId("access-state"),
    feed: byId("feed"),
    feedState: byId("feed-state"),
    room: byId("room-description"),
    mapGrid: byId("map-grid"),
    mapSpace: byId("map-space"),
    mapPosition: byId("map-position"),
    mapStatus: byId("map-status"),
    mapNote: byId("map-note"),
    mapLegend: byId("map-legend"),
    commandInput: byId("command-input"),
    send: byId("send-button"),
    commandHint: byId("command-hint"),
  };

  const quickActions = [...document.querySelectorAll("[data-command]")];
  const NAME_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{0,23}$/;
  const POLL_DELAY_MS = 2200;
  const MAX_RATE_RETRY_MS = 30000;
  const MAX_FEED_ITEMS = 160;
  const MAX_COMMAND_HISTORY = 50;
  let requestSerial = 0;
  let current = null; // In-memory only: no session, endpoint, or token storage.
  let connecting = null; // Prevent overlapping joins while current is still null.
  // One uncertain mutation may outlive a disconnect in this page's memory.
  // Never retain its bearer: a reconnect must independently authenticate.
  let unresolvedCommand = null;

  function setNote(message, error = false) {
    ui.note.textContent = message;
    ui.note.classList.toggle("is-error", error);
  }

  function setConnectionState(connected) {
    ui.dot.classList.toggle("is-connected", connected);
    ui.mode.classList.toggle("is-live", connected);
    ui.feedState.classList.toggle("is-live", connected);
    ui.mode.textContent = connected ? "CONNECTED TO HOST" : "OFFLINE PREVIEW";
    ui.feedState.textContent = connected ? "HOST CONNECTED" : "NOT LIVE";
    ui.connectionStatus.textContent = connected ? "Connected — receiving host state" : "Offline — no world connected";
    ui.connect.disabled = connected;
    ui.disconnect.disabled = !connected;
    ui.disconnect.textContent = "Disconnect";
    ui.commandInput.disabled = !connected;
    ui.send.disabled = !connected;
    ui.commandHint.textContent = connected ? "Host commands · ↑/↓ recall up to 50 recent entries" : "Connect to issue commands";
    quickActions.forEach((button) => { button.disabled = !connected; });
    [ui.hostUrl, ui.playerName, ui.accessToken].forEach((input) => { input.disabled = connected; });
  }

  function appendFeed({ category = "system", label, message, icon = "◇" }) {
    const nearBottom = ui.feed.scrollTop + ui.feed.clientHeight >= ui.feed.scrollHeight - 54;
    const li = document.createElement("li");
    li.className = category === "herald" ? "feed-item feed-response feed-pvp-death"
      : category === "emote" ? "feed-item feed-system feed-emote"
      : category === "ooc" ? "feed-item feed-system feed-ooc"
      : category === "crewchat" ? "feed-item feed-response feed-crewchat"
      : category === "private-error" ? "feed-item feed-error feed-crewchat"
      : "feed-item feed-" + (category === "error" ? "error" : category === "response" ? "response" : "system");
    const symbol = document.createElement("span");
    symbol.className = "feed-icon";
    symbol.setAttribute("aria-hidden", "true");
    symbol.textContent = icon;
    const content = document.createElement("div");
    const heading = document.createElement("small");
    heading.textContent = label;
    const text = document.createElement("p");
    // All remote player text is inert, never inserted as HTML.
    text.textContent = String(message);
    content.append(heading, text);
    li.append(symbol, content);
    ui.feed.append(li);
    while (ui.feed.children.length > MAX_FEED_ITEMS) ui.feed.firstElementChild.remove();
    if (nearBottom) ui.feed.scrollTop = ui.feed.scrollHeight;
  }

  function resetFeed() {
    ui.feed.replaceChildren();
  }

  function isPrivateCommand(command) {
    return typeof command === "string" && /^crewchat(?:\s|$)/i.test(command);
  }

  function clearPrivateFeed() {
    // Unlike public room/world narration, the private group's transcript
    // must not remain on screen after its authenticated session ends.
    ui.feed.replaceChildren(...[...ui.feed.children].filter((item) =>
      !item.className.split(/\s+/).includes("feed-crewchat")));
  }

  function clearPrivateSessionState(session) {
    const priorLength = session.commandHistory.length;
    session.commandHistory = session.commandHistory.filter((command) => !isPrivateCommand(command));
    if (session.commandHistory.length !== priorLength) session.historyIndex = null;
    if (isPrivateCommand(session.commandDraft)) session.commandDraft = "";
    if (isPrivateCommand(ui.commandInput.value)) ui.commandInput.value = "";
    if (session.pendingCommandRetry && isPrivateCommand(session.pendingCommandRetry.command)) {
      session.pendingCommandRetry = null;
    }
    // An in-flight POST is allowed to finish for server receipt integrity,
    // but its response must not reappear after the permission epoch changes.
  }

  function normalizeEndpoint(value) {
    let url;
    try { url = new URL(value.trim()); }
    catch { throw new Error("Enter a complete host URL, including https://."); }
    if (url.username || url.password || url.search || url.hash) {
      throw new Error("Use a host base URL without credentials, query parameters, or fragments.");
    }
    if (!['https:', 'http:'].includes(url.protocol)) throw new Error("Only HTTP(S) REALM hosts are supported.");
    // A public GitHub Pages HTTPS client cannot depend on mixed-content HTTP.
    if (window.location.protocol === "https:" && url.protocol !== "https:") {
      throw new Error("An HTTPS page requires an HTTPS REALM host. A public tunnel is not configured by this page.");
    }
    // Never send bearer secrets over cleartext to a network host.
    if (url.protocol === "http:" && !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname.toLowerCase())) {
      throw new Error("HTTP is permitted only for loopback development hosts. Use HTTPS for remote worlds.");
    }
    if (url.pathname !== "/" && url.pathname !== "") {
      throw new Error("Enter the host origin only; API routes are added automatically.");
    }
    return url.origin;
  }

  async function request(session, path, options = {}) {
    const headers = { "Accept": "application/json" };
    if (!options.unauthenticated) headers.Authorization = "Bearer " + session.token;
    if (options.body) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(session.base + path, {
        method: options.body ? "POST" : "GET",
        headers,
        body: options.body ? JSON.stringify(options.body) : undefined,
        credentials: "omit",
        cache: "no-store",
        signal: options.signal || session.abortController.signal,
      });
    } catch {
      const error = new Error("The host could not be reached. Check its address, HTTPS, CORS origin, and network access.");
      // Fetch rejected before an HTTP response existed (e.g. a reset socket).
      // Keep this distinct from malformed JSON or a real 401/403 response.
      error.transportFailure = true;
      throw error;
    }
    let data;
    try { data = await response.json(); }
    catch {
      // An overloaded proxy can emit an empty/non-JSON 503. Preserve the
      // HTTP status so the client can back off without severing a session.
      if (response.ok) throw new Error("The host returned a response that was not JSON.");
      data = {};
    }
    if (!response.ok) {
      const detail = typeof data?.error === "string" ? data.error : "Request rejected by the host";
      const error = new Error(detail + " (HTTP " + response.status + ")");
      error.status = response.status;
      if (response.status === 429 || response.status === 503) {
        // Only bounded numeric Retry-After seconds affect host-feed polling.
        const header = response.headers?.get?.("Retry-After");
        const seconds = typeof header === "string" && /^\d{1,5}$/.test(header.trim())
          ? Number(header.trim()) : NaN;
        error.retryAfterMs = Number.isFinite(seconds)
          ? Math.max(POLL_DELAY_MS, Math.min(MAX_RATE_RETRY_MS, seconds * 1000))
          : POLL_DELAY_MS * 2;
      }
      throw error;
    }
    return data;
  }

  function parseCursor(value, fallback = 0) {
    const n = Number(value);
    return Number.isSafeInteger(n) && n >= 0 ? n : fallback;
  }

  function stopSession({ note, discardPrivateRetry = false } = {}) {
    if (connecting && connecting !== current) {
      // An in-flight join may outlive a user's cancellation. Clear the token
      // and invalidate its session before any delayed response can join us.
      connecting.abortController.abort();
      connecting.token = "";
      connecting = null;
    }
    if (current) {
      if (connecting === current) connecting = null;
      const retry = current.pendingCommandRetry || current.inflightCommand;
      unresolvedCommand = retry && current.worldId &&
        !(discardPrivateRetry && isPrivateCommand(retry.command)) ? {
        base: current.base, worldId: current.worldId, player: current.player,
        command: retry.command, actionId: retry.actionId,
      } : null;
      clearInterval(current.timer);
      // Cancel any in-flight room, map, feed, or command response belonging to
      // this session, and release its credential before a future reconnect.
      current.abortController.abort();
      current.token = "";
      // Recalled input belongs to this browser session only.
      current.commandHistory.length = 0;
      current.historyIndex = null;
      current.commandDraft = "";
      current = null;
    }
    if (discardPrivateRetry && unresolvedCommand && isPrivateCommand(unresolvedCommand.command)) {
      unresolvedCommand = null;
    }
    // Drop the only DOM copy of the access token on disconnect.
    ui.accessToken.value = "";
    clearPrivateFeed();
    setConnectionState(false);
    ui.connect.textContent = "Connect";
    ui.worldName.textContent = "Unconnected";
    ui.player.textContent = "None";
    ui.worldState.textContent = "Not loaded";
    ui.accessState.textContent = "Host authorization required";
    ui.room.textContent = "No character is connected.\n\nThe current room will appear here when you enter a hosted REALM.";
    ui.commandInput.value = "";
    if (window.RealmMap) window.RealmMap.setUnavailable(mapElements, "Connect a compatible host to view its authorized map.");
    if (note) setNote(note);
  }

  const mapElements = { grid: ui.mapGrid, space: ui.mapSpace, position: ui.mapPosition,
    status: ui.mapStatus, note: ui.mapNote, legend: ui.mapLegend };

  async function refreshMap(session, { afterCommand = false } = {}) {
    if (!window.RealmMap || current !== session || session.mapUnsupported) return;
    if (session.mapPolling) {
      if (afterCommand) session.mapRefreshPending = true;
      return;
    }
    session.mapPolling = true;
    try {
      const payload = await request(session, "/api/v1/map?player=" + encodeURIComponent(session.player));
      // A command may have committed while this older response was pending.
      if (current !== session || session.mapRefreshPending) return;
      window.RealmMap.renderMap(document, mapElements, payload);
    } catch (error) {
      if (current !== session || session.mapRefreshPending) return;
      // A 404 may mean this particular room has no mapped space; retry after travel.
      if (error.status === 501) session.mapUnsupported = true;
      window.RealmMap.setUnavailable(mapElements, error.status === 404
        ? "No map is available here. This host may not provide maps, or the location may be unmapped."
        : session.mapUnsupported ? "This host does not support maps yet. Commands still work."
          : "Host map update failed: " + error.message);
    } finally {
      session.mapPolling = false;
      session.mapUpdated = Date.now();
      if (current === session && session.mapRefreshPending) {
        session.mapRefreshPending = false;
        await refreshMap(session);
      }
    }
  }

  async function refreshRoom(session) {
    const revision = ++session.roomRevision;
    try {
      const room = await request(session, "/api/v1/room?player=" + encodeURIComponent(session.player));
      if (current !== session || revision !== session.roomRevision) return;
      ui.room.textContent = typeof room.text === "string" ? room.text : "The host did not return a room description.";
    } catch (error) {
      if (current !== session || revision !== session.roomRevision) return;
      throw error;
    }
  }

  function describeEvent(event) {
    const who = typeof event.actor === "string" ? event.actor : "World";
    const kind = typeof event.kind === "string" ? event.kind : "event";
    const message = typeof event.text === "string" ? event.text : "An event occurred.";
    // A host-authored global PK notice is not a room event. Even if a future
    // host accidentally supplies a room field, never echo its location here.
    // The host decides recipient authorization; this client only renders text.
    if (kind === "pvp_death") {
      return { category: "herald", label: "REALM / DEATH HERALD", message, icon: "⚔" };
    }
    // Channel membership, roles, sender identity, and visibility are governed
    // by the host. Its preformatted message stays inert text, while headings
    // are fixed so untrusted room/role fields cannot impersonate UI labels.
    if (kind === "emote") return { category: "emote", label: "SCENE / EMOTE", message, icon: "✧" };
    if (kind === "ooc") return { category: "ooc", label: "WORLD / OOC", message, icon: "◉" };
    if (kind === "crewchat") return { category: "crewchat", label: "GROUP / PRIVATE", message, icon: "◆" };
    const room = typeof event.room === "string" ? " · " + event.room : "";
    return { category: "system", label: who + " / " + kind + room, message, icon: kind === "say" ? "✦" : "◇" };
  }

  async function poll(session) {
    if (current !== session || session.polling || Date.now() < session.pollPauseUntil) return;
    session.polling = true;
    try {
      const data = await request(session, "/api/v1/feed?player=" + encodeURIComponent(session.player) + "&after=" + session.cursor);
      if (current !== session) return;
      if (!Array.isArray(data.events)) throw new Error("Host feed response has no events array.");
      const hasRevision = Object.prototype.hasOwnProperty.call(data, "channel_revision");
      if (hasRevision && (!Number.isSafeInteger(data.channel_revision) || data.channel_revision < 0)) {
        stopSession({ note: "The host returned an invalid channel permission revision; disconnected.", discardPrivateRetry: true });
        return;
      }
      if (session.channelRevision !== null && !hasRevision) {
        stopSession({ note: "The host stopped attesting private channel access; disconnected.", discardPrivateRetry: true });
        return;
      }
      if (!hasRevision && data.events.some((event) => event && event.kind === "crewchat")) {
        // Legacy public-only hosts omit this field; accepting a purported
        // private event without an epoch leaves no revocation witness.
        stopSession({ note: "The host did not attest private channel access; disconnected.", discardPrivateRetry: true });
        return;
      }
      if (session.channelRevision !== null && data.channel_revision !== session.channelRevision) {
        clearPrivateFeed();
        clearPrivateSessionState(session);
      }
      session.channelRevision = hasRevision ? data.channel_revision : null;
      for (const event of data.events) {
        const id = parseCursor(event.id, -1);
        if (id <= session.cursor) continue;
        appendFeed(describeEvent(event));
        session.cursor = id;
      }
      session.cursor = Math.max(session.cursor, parseCursor(data.cursor, session.cursor));
      session.failures = 0;
      session.throttled = false;
      session.overloaded = false;
      session.overloadFailures = 0;
      session.pollPauseUntil = 0;
      if (Date.now() - session.mapUpdated > 12000) await refreshMap(session);
    } catch (error) {
      if (current !== session) return;
      if ([401, 403, 409].includes(error.status)) {
        stopSession({ note: "Host access or private channel membership changed; disconnected.", discardPrivateRetry: true });
        return;
      }
      if (error.status === 429) {
        session.overloadFailures = 0;
        session.overloaded = false;
        session.pollPauseUntil = Date.now() + error.retryAfterMs;
        session.failures = 0;
        if (!session.throttled) appendFeed({ category: "system", label: "HOST / RATE LIMIT", message: "The host is busy. Feed updates will resume after its retry interval.", icon: "◇" });
        session.throttled = true;
        return;
      }
      if (error.status === 503 || error.status === 408 || error.transportFailure === true) {
        // 503 admission failure and 408 request timeout are transient; the
        // feed GET is safe to retry. A pre-header transport reset needs the
        // same bounded backoff, but is not proof the host is overloaded.
        const transportFailure = error.transportFailure === true;
        session.overloadFailures++;
        if (session.overloadFailures >= 4) {
          stopSession({ note: transportFailure
            ? "The host connection was interrupted repeatedly; disconnected. Check its address, HTTPS, CORS, and network access."
            : "The host was busy repeatedly; disconnected. Reconnect when it is available." });
          return;
        }
        const backoff = Math.min(MAX_RATE_RETRY_MS, POLL_DELAY_MS * (2 ** session.overloadFailures));
        session.pollPauseUntil = Date.now() + Math.max(backoff,
          error.status === 503 ? (error.retryAfterMs || 0) : 0);
        session.failures = 0;
        if (!session.overloaded) appendFeed({
          category: "system", label: transportFailure ? "HOST / CONNECTION" : "HOST / BUSY",
          message: transportFailure
            ? "The host connection was interrupted before a response. Feed updates will resume after a bounded delay."
            : "The host is temporarily unavailable. Feed updates will resume after a bounded delay.",
          icon: "◇",
        });
        session.overloaded = true;
        return;
      }
      session.overloadFailures = 0;
      session.overloaded = false;
      session.failures++;
      if (session.failures === 1) appendFeed({ category: "error", label: "CONNECTION INTERRUPTED", message: error.message, icon: "!" });
      if (session.failures >= 3) {
        stopSession({ note: "Host polling failed repeatedly; disconnected. Reconnect to resume." });
      }
    } finally {
      session.polling = false;
    }
  }

  async function connect(event) {
    event.preventDefault();
    if (current || connecting) return;
    if (!ui.connectionForm.reportValidity()) return;
    const player = ui.playerName.value.trim();
    const token = ui.accessToken.value.trim();
    if (!NAME_PATTERN.test(player)) return setNote("Player names must use letters, digits, underscores, or hyphens, starting with a letter.", true);
    if (!token) return setNote("Your host access token is required.", true);
    let base;
    try { base = normalizeEndpoint(ui.hostUrl.value); }
    catch (error) { return setNote(error.message, true); }

    ui.connect.disabled = true;
    ui.connect.textContent = "Connecting…";
    ui.disconnect.textContent = "Cancel";
    setNote("Contacting the host…");
    const session = { base, token, player, abortController: new AbortController(), cursor: 0, polling: false, failures: 0,
      throttled: false, overloaded: false, overloadFailures: 0, pollPauseUntil: 0, roomRevision: 0,
      channelRevision: null,
      commandPending: false, pendingCommandRetry: null, inflightCommand: null,
      commandHistory: [], historyIndex: null, commandDraft: "",
      mapUnsupported: false, mapPolling: false, mapRefreshPending: false, mapUpdated: 0, timer: null };
    connecting = session;
    // The cancellable in-memory session now owns the sole token copy.
    ui.accessToken.value = "";
    ui.disconnect.disabled = false;
    try {
      const health = await request(session, "/api/v1/health", { unauthenticated: true });
      if (connecting !== session) return;
      if (health.ok !== true || typeof health.world_id !== "string") throw new Error("The endpoint is not a compatible REALM gateway.");
      session.worldId = health.world_id;
      const joined = await request(session, "/api/v1/join", { body: { player } });
      if (connecting !== session) return;
      // A 200 alone is not a successful join: the cursor is required to
      // distinguish newly visible events from the room's previous history.
      // Older compatible hosts may omit `ok` or `text`, but an explicit
      // negative acknowledgment or missing/invalid cursor cannot connect.
      if (!joined || !Object.prototype.hasOwnProperty.call(joined, "cursor") ||
          !Number.isSafeInteger(joined.cursor) || joined.cursor < 0 ||
          (Object.prototype.hasOwnProperty.call(joined, "ok") && joined.ok !== true)) {
        throw new Error("Host join response lacks a valid success cursor.");
      }
      session.cursor = joined.cursor;
      current = session;
      resetFeed();
      setConnectionState(true);
      const retry = unresolvedCommand;
      unresolvedCommand = null;
      if (retry && retry.base === base && retry.worldId === health.world_id && retry.player === player) {
        session.pendingCommandRetry = { command: retry.command, actionId: retry.actionId };
        ui.commandInput.value = retry.command;
      }
      // Keep the token in the session object only, not in the password input DOM.
      ui.accessToken.value = "";
      ui.worldName.textContent = health.world_id;
      ui.player.textContent = player;
      ui.worldState.textContent = "Persistent host state";
      ui.accessState.textContent = "Session connected";
      setNote("Connected. Your browser token is held in memory only and will be cleared when you disconnect.");
      appendFeed({ label: "HOST / JOIN", message: typeof joined.text === "string" ? joined.text : "Connected to host.", icon: "✦" });
      if (session.pendingCommandRetry) appendFeed({ category: "system", label: "HOST / RETRY",
        message: "Your last command may have completed before disconnect. Press Send to retry the unchanged command with its original action ID, or edit it to start a new action.", icon: "◇" });
      try { await refreshRoom(session); }
      catch (error) {
        if (current === session) appendFeed({ category: "error", label: "ROOM UPDATE", message: error.message, icon: "!" });
      }
      if (current !== session) return;
      await refreshMap(session);
      if (current !== session) return;
      session.timer = setInterval(() => poll(session), POLL_DELAY_MS);
      ui.commandInput.focus();
    } catch (error) {
      if (connecting === session || current === session) {
        stopSession({ note: "Connection failed: " + error.message });
        ui.note.classList.add("is-error");
      }
    } finally {
      // A disconnected attempt may finish after a newer connection starts.
      // Its finally block must not change the new attempt's controls.
      if (connecting === session) {
        connecting = null;
        ui.connect.textContent = "Connect";
        if (!current) ui.connect.disabled = false;
      }
    }
  }

  async function sendCommand(event) {
    event.preventDefault();
    const session = current;
    if (!session) return;
    if (session.commandPending) return;
    // Acquire before any command input getter or UI/history setter can reenter.
    session.commandPending = true;
    try {
      // Synchronous input/UUID/DOM setup must share the finally boundary: a
      // thrown getter or setter must not poison the current session's latch.
      const command = ui.commandInput.value.trim();
      if (!command) return;
      const privateChat = isPrivateCommand(command);
      const commandRevision = session.channelRevision;
      const prior = session.pendingCommandRetry;
      const actionId = prior && prior.command === command ? prior.actionId
        : (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : "web-" + Date.now() + "-" + (++requestSerial));
      if (prior && prior.command !== command) session.pendingCommandRetry = null;
      ui.send.disabled = true;
      ui.commandInput.disabled = true;
      // No history entry for a command that failed before reaching dispatch.
      session.commandHistory.push(command);
      if (session.commandHistory.length > MAX_COMMAND_HISTORY) session.commandHistory.shift();
      session.historyIndex = null;
      session.commandDraft = "";
      session.inflightCommand = { command, actionId };
      try {
        const answer = await request(session, "/api/v1/command", { body: { player: session.player, command, action_id: actionId } });
        if (current !== session) return;
        session.inflightCommand = null;
        session.pendingCommandRetry = null;
        if (privateChat && commandRevision !== session.channelRevision) {
          appendFeed({ label: "HOST / GROUP CHANGED", message: "Private channel access changed while the command was in flight; its response was hidden.", icon: "◇" });
        } else {
          appendFeed(privateChat
            ? { category: "crewchat", label: "GROUP / PRIVATE", message: answer.text || "(No response)", icon: "◆" }
            : { category: "response", label: session.player + " / " + command, message: answer.text || "(No response)", icon: "›" });
        }
        ui.commandInput.value = "";
        if (/^(quit|exit)$/i.test(command)) {
          stopSession({ note: "Your character's last position remains saved on the host." });
          return;
        }
        try { await refreshRoom(session); }
        catch (error) {
          if (current === session) appendFeed({ category: privateChat ? "private-error" : "error",
            label: privateChat ? "GROUP / ROOM UPDATE" : "ROOM UPDATE", message: error.message, icon: "!" });
        }
        if (current !== session) return;
        await poll(session);
        if (current !== session) return;
        await refreshMap(session, { afterCommand: true });
      } catch (error) {
        if (current === session) {
          if (privateChat && commandRevision !== session.channelRevision) {
            session.pendingCommandRetry = null;
            appendFeed({ label: "HOST / GROUP CHANGED", message: "A private command's outcome is uncertain after channel access changed. Do not submit it again without checking with the host.", icon: "◇" });
            return;
          }
          if (error.status === 503 || error.status === 408 || error.status == null) {
            // Never auto-replay a mutating POST after an ambiguous response.
            // A manual retry with unchanged text reuses its durable action ID.
            session.pendingCommandRetry = { command, actionId };
            appendFeed({
              category: "system", label: "HOST / RETRY",
              message: "The command response was interrupted. If you retry the same command manually, its action ID will be reused.",
              icon: "◇",
            });
          } else {
            session.pendingCommandRetry = null;
            appendFeed({ category: privateChat ? "private-error" : "error",
              label: privateChat ? "GROUP / COMMAND REJECTED" : "COMMAND REJECTED", message: error.message, icon: "!" });
          }
        }
      }
    } finally {
      session.commandPending = false;
      session.inflightCommand = null;
      if (current === session) {
        ui.send.disabled = false;
        ui.commandInput.disabled = false;
        ui.commandInput.focus();
      }
    }
  }

  function recallCommand(event) {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    const session = current;
    if (!session || session.commandHistory.length === 0) return;
    if (event.key === "ArrowUp") {
      if (session.historyIndex === null) {
        session.commandDraft = ui.commandInput.value;
        session.historyIndex = session.commandHistory.length - 1;
      } else if (session.historyIndex > 0) {
        session.historyIndex--;
      }
    } else {
      if (session.historyIndex === null) return;
      if (session.historyIndex < session.commandHistory.length - 1) {
        session.historyIndex++;
      } else {
        session.historyIndex = null;
        ui.commandInput.value = session.commandDraft;
        event.preventDefault();
        return;
      }
    }
    ui.commandInput.value = session.commandHistory[session.historyIndex];
    event.preventDefault();
  }

  ui.connectionForm.addEventListener("submit", connect);
  ui.commandForm.addEventListener("submit", sendCommand);
  ui.commandInput.addEventListener("keydown", recallCommand);
  ui.commandInput.addEventListener("input", () => {
    if (current) current.historyIndex = null;
  });
  ui.disconnect.addEventListener("click", () => stopSession({ note: "Disconnected locally. The world remains on your host." }));
  quickActions.forEach((button) => button.addEventListener("click", () => {
    if (!current) return;
    // A suggested command is a new draft, not a continuation of ↑/↓ recall.
    current.historyIndex = null;
    current.commandDraft = "";
    ui.commandInput.value = button.dataset.command;
    ui.commandInput.focus();
  }));
  setConnectionState(false);
})();
