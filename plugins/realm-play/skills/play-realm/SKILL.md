---
name: play-realm
description: Use when the user asks you to play, explore, or interact with a locally configured REALM world through discovered REALM MCP tools.
---

# Playing REALM

Use only the **discovered** local `realm-local` MCP tools when interacting with the configured game. A plugin manifest does not mean the server is connected.

1. Verify the actual tools are available. If the local server is unconfigured or disconnected, report that fact without claiming to have visited the game.
2. Call `realm_look` at the beginning of a session, after uncertain state, and when another player may have altered the scene. Report its actual output. Use `realm_inventory` if objects are relevant.
3. Use `realm_move` for room movement and `realm_say` for in-game speech. Select destinations and words based on observed game state; never invent rooms, inventory, player identities, or action receipts.
4. Respect the server's fixed actor and world configuration. Do not request arbitrary database paths or actors as action parameters. A failed or rejected tool call is not a successful game action.
5. Distinguish **what happened in the world** from your interpretation and plans. Ask before disruptive, irreversible, or externally consequential actions if such tools are added later.
6. Never expose host environment variables, player credentials, private world files, or internal game database details in public chat or in-game speech.

This plugin supplies local **tools**, not autonomous scheduling, game knowledge, neural NPC weights, or a public bridge from cloud ChatGPT to a local machine. The game engine remains authoritative for state changes.
