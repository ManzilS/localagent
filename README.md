# LocalAgent

A local-first personal AI assistant that works with files, documents, websites, and desktop apps. LocalAgent combines a local model through Ollama with optional connected model providers, a desktop interface, and tools for carrying out tasks.

**Status: active development (0.4.0).** This is an experimental project, not a finished product. Features and integrations vary in maturity. Automated tests do not establish reliable operation across every app, model, or operating system.

## What it does

- Reads and writes files, runs code, researches the web, and processes documents and images.
- Operates browsers and desktop apps through dedicated tools.
- Supports local models, optional subscription CLI bridges, and API-based providers.
- Provides a task board, scheduled automations, heartbeat checklists, and a proactive suggestion feed.
- Stores local memory and reusable `SKILL.md` skills.
- Connects to external services through built-in connectors and MCP servers.
- Includes Telegram control, remote-access support, and optional local speech recognition.

Example tasks include organizing a folder of documents, summarizing a report into notes, researching a topic, or preparing a spreadsheet. Task quality depends on the model, permissions, and tools available.

## Run from source

Python 3.10 or later is required; Python 3.12 is the development baseline. Use a virtual environment. Local inference also requires Ollama and a downloaded model; the setup flow helps configure these.

### Windows (PowerShell)

```powershell
git clone https://github.com/ManzilS/localagent.git
cd localagent
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[all,dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\localagent.exe app
```

### macOS / Linux

```bash
git clone https://github.com/ManzilS/localagent.git
cd localagent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[all,dev]"
python -m playwright install chromium
localagent app
```

Desktop support can require OS-specific GUI libraries and accessibility permissions. For a smaller installation, use `pip install -e ".[dev]"` and `localagent web` or the terminal interface. Browser tools still need the Playwright Chromium installation. The optional `all` extra installs desktop, document, vision, and voice dependencies.

### Commands

Run these with the virtual environment activated, or use its `localagent` executable directly:

```text
localagent app                 Desktop interface
localagent web                 Local browser interface
localagent                     Terminal chat
localagent run "your task"     Run one task
localagent setup               Configure models and workspace
localagent doctor              Check the environment
localagent brains              Inspect connected model bridges
localagent daemon              Run background services
localagent trash               Inspect recoverable file removals
```

## Models and data

The interface offers Fast, Balanced, and Best modes (internally `quick`, `auto`, and `deep`). Fast is intended for local inference; the other modes can route work to connected providers. Users can also select a provider explicitly.

The implementation includes an Ollama provider, OpenAI-compatible and Anthropic API providers, a Codex CLI bridge, and a Gemini/Antigravity CLI bridge. External integrations depend on installed tools, authentication, account access, and upstream behavior. Their presence in this project is not an endorsement or a guarantee of subscription availability.

**Local-first does not mean every task stays offline.** Web tools, connectors, remote features, and connected model providers send data outside the computer. A connected model can receive task context and tool results. Choose providers and enabled tools accordingly.

Configuration normally lives in `~/.localagent/config.yaml`; `LOCALAGENT_HOME` overrides the application data directory. Model candidates and hardware profiles are defined in [`config.py`](localagent/config.py). Keep application state, credentials, browser profiles, and personal documents out of source control.

## Safety model and limits

LocalAgent includes several safeguards:

- File-operation shims and Python hooks route supported removals and overwrites into recoverable trash.
- Command screening rejects recognized destructive operations.
- Approval prompts gate selected risky actions.
- Outside content is labeled as untrusted; additional approval checks apply after reading it.
- The local HTTP server uses authentication and host/origin checks.

These are **guardrails, not an operating-system sandbox or a guarantee against data loss**. Programs can bypass supported file hooks, app actions have their own deletion behavior, and model-driven actions can be wrong. Use a disposable workspace or separate OS account when evaluating unfamiliar tasks. Review the configured writable roots and approval rules before allowing access to important files.

Known limitations:

- Small local models can struggle with long tasks and desktop control; speed depends heavily on hardware.
- The Gemini/Antigravity bridge does not yet have verified enforcement that disables the CLI's own tools. Treat it as experimental.
- Third-party connectors, skills, and model output remain untrusted despite scanning and approval checks.
- Remote access and external integrations require their own configuration and validation.
- Cross-platform packaging is configured, but a successful source test run does not verify packaged applications.

## Development

```bash
python -m pip install -e ".[all,dev]"
python -m playwright install chromium
python -m pytest -q
```

Tests exercise tools, storage, safety rules, connectors, browser interactions, scheduling, and provider behavior using fixtures and fake services. They do not require live model subscriptions. Browser tests require installed Chromium. Test configuration disables the real OS keychain.

The test workflow runs on pushes and pull requests. The separate app-build workflow runs manually or on `v*` tags and attempts to package Windows, macOS, and Linux builds with PyInstaller. Build artifacts are available only after a successful workflow run; no prebuilt download is promised by this README.

## Project layout

| Path | Purpose |
| --- | --- |
| `localagent/agent.py`, `llm.py`, `bridges.py` | Agent loop, providers, routing, and CLI bridges |
| `localagent/tools/` | Files, shell, web, documents, browser, desktop, and delegation tools |
| `localagent/connections/`, `mcp.py` | Service integrations and MCP client |
| `localagent/webui.py`, `web/`, `app.py` | HTTP server, interface, and desktop window |
| `localagent/tasks.py`, `automations.py`, `proactive.py` | Task management, scheduling, and suggestions |
| `localagent/safety.py`, `guard.py`, `trash.py` | Action checks, untrusted-content handling, and recovery |
| `localagent/skills_builtin/`, `memory_store.py` | Built-in skills and local memory |
| `tests/` | Automated test suite |
| `packaging/` | Desktop packaging configuration |

## Contributing

Issues and focused pull requests are welcome. Include reproduction steps, operating system, Python version, and relevant redacted output. Do not upload credentials, personal documents, browser session data, or complete unreviewed logs. Explain how changes were verified and distinguish automated tests from live integration checks.
