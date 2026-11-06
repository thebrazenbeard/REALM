```markdown
# REALM Engine
> **R**untime **E**ngine for **A**utonomous **L**anguage **M**atrix

REALM is an open-source, local-first engine designed to run persistent, multi-user text-based virtual worlds powered by autonomous AI agents. 

Unlike traditional MUDs that rely on rigid verb parsers and pre-scripted NPC dialog trees, a **REALM** host runs dynamic background agent loops. Non-player characters possess persistent memory, personal drives, and systemic relationship graphs, allowing history to unfold organically whether players are actively logged in or away.

---

## 🌟 Key Features

* **Multi-Instance Support:** Spin up, run, and bridge multiple isolated REALMs on a single host machine with independent state databases, rule systems, and agent pools.
* **Autonomous Agent Runtimes:** NPCs operate on continuous event loops driven by intrinsic goals rather than static reaction triggers.
* **Local-First Architecture:** Complete offline privacy. Connect to local LLM inference engines (e.g., Ollama, vLLM) or route to cloud APIs via standardized adapters.
* **Emergent World State:** Event-driven history logs ensure actions carry lasting consequences across room states, faction alignments, and environmental physics.
* **Zero-Friction Client Connection:** Play via standard WebSocket, SSH terminal sessions, or modern web interfaces.

---

## 🏗️ Architecture & Core Concepts

Every local deployment consists of the core **REALM Daemon**, which manages background event loops, state persistence, and inter-process communication:


```

```
                  +-----------------------------------+
                  |      Host Machine (Local Daemon)  |
                  |                                   |
                  |  +----------+       +----------+  |
                  |  | REALM 01 |       | REALM 02 |  |
                  |  | (Cyber)  |       | (Fantasy)|  |
                  |  +----+-----+       +----+-----+  |
                  +-------|------------------|--------+
                          |                  |
       +------------------+                  +------------------+
       |                                                        |
[Local LLM / API]                                        [SQLite / Postgres]

```

(Agent Reasoning & Logic)                                 (State, Logs & Memory)

```

* **The Matrix (State Layer):** An immutable log of world facts, item locations, room topology, and historical ticks.
* **Agent Drivers:** Isolated worker runtime processes that sample the world matrix, update NPC context windows, and emit structured action intents.
* **Bridge Protocol:** Allows player characters to travel between distinct local REALM instances via configured gateway portals.

---

## 🚀 Quick Start

### Prerequisites
* Python 3.11+ or Rust Toolchain
* Local LLM runner (e.g., [Ollama](https://ollama.ai)) or an API key for external inference.

### Installation & Initialization

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/your-username/realm-engine.git](https://github.com/your-username/realm-engine.git)
   cd realm-engine

```

2. **Initialize the local host daemon:**
```bash
realm host init

```


3. **Create your first REALM instance:**
```bash
realm create cyberpunk-slums --template sci-fi-base --port 7777

```


4. **Create a second isolated REALM instance:**
```bash
realm create high-fantasy-citadel --template fantasy-base --port 7778

```


5. **Start the host engine:**
```bash
realm host start --all

```



---

## 🎮 Managing Local Instances

Use the CLI to control your locally hosted worlds:

```bash
# List all active REALM instances running on your machine
realm list

# Tail agent activity and world ticks for a specific instance
realm logs cyberpunk-slums --follow

# Inspect active NPCs and their current internal state/drives
realm agents inspect high-fantasy-citadel --npc "Guard_01"

# Stop a specific instance without affecting others
realm stop cyberpunk-slums

```

---

## 🛠️ Configuration Example (`realm.config.toml`)

Each instance operates under its own isolated configuration:

```toml
[realm]
id = "cyberpunk-slums"
name = "Neo-Veridia Sector 4"
max_players = 32

[inference]
provider = "ollama"
model = "llama3:8b"
base_url = "http://localhost:11434"

[agents]
tick_rate_seconds = 5
max_active_agents = 20
memory_retention_mode = "vector_graph"

```

---

## 🤝 Contributing

Contributions are welcome! Please check out [CONTRIBUTING.md](CONTRIBUTING.md) for details on submitting pull requests, writing custom agent drivers, and developing world templates.

## 📄 License

This project is licensed under the my License - see the [LICENSE](https://www.google.com/search?q=LICENSE) file for details.

```

<FollowUp label="Want to draft the CLI command structure or JSON event schema for agent ticks?" query="Draft a specification for the JSON event schema used by REALM agent ticks and state updates."/>

```
