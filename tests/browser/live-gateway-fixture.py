"""Explicitly authored generic geography fixture for local browser E2E tests.

Runs on loopback port 0; never deploys, tunnels, or loads private worldpack data.
"""

import sys

from realm.gateway import Gateway, GatewayServer


def main() -> None:
    db, token = sys.argv[1:3]
    gateway = Gateway(db, world_id="browser-integration-test")
    geo = gateway.geography
    geo.add_place("test-universe", "Test universe", "universe")
    geo.add_place("test-system", "Test system", "system", "test-universe")
    geo.add_place("test-planet", "Test planet", "planet", "test-system")
    geo.add_place("test-region", "Test region", "region", "test-planet")
    geo.add_place("test-town", "Test town", "settlement", "test-region")
    geo.add_place("atrium", "Test atrium", "room", "test-town", x=0, y=0)
    geo.add_place("corridor", "Test corridor", "room", "test-town", x=2, y=0)
    geo.set_map("test-town", [".^.", "..~"])
    if hasattr(gateway, "credentials"):
        # Auth v2 binds the bearer to the player; never reuse a shared world key.
        player_token = gateway.credentials.provision("Explorer")
        other_token = gateway.credentials.provision("Other")
        server = GatewayServer(gateway, ("http://127.0.0.1:8000",), port=0)
        mode = "player"
    else:
        # Explicit legacy compatibility for the separately-developed geo branch.
        player_token, other_token, mode = token, "none", "legacy"
        server = GatewayServer(gateway, token, ("http://127.0.0.1:8000",), port=0)
    print(f"READY {server.server_port} {player_token} {other_token} {mode}", flush=True)
    server.serve_forever(poll_interval=0.05)


if __name__ == "__main__":
    main()
