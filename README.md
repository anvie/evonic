# Evonic

> **Design. Deploy. Orchestrate.**  
> *Your Models. Your Rules. Your Swarm.*

Evonic is an **agentic AI framework** for designing, building, and orchestrating intelligent agents from concept to production. It empowers you to define every aspect of an agent — its **model**, **tools**, **knowledge base**, **channels**, and **skills** — and compose them into multi-agent systems that operate autonomously across distributed environments.

**Full documentation:** [evonic.dev](https://evonic.dev)

<p align="center">
  <img src="static/img/mascot-large.png" alt="Evonic Logo" width="200">
</p>

<p align="center">
  <img src="static/img/evonic-web.jpg" alt="Evonic Web UI Screenshot" width="600">
</p>

---

## Three Core Differentiators

Evonic is not just another agent framework. Three architectural decisions set it apart:

### 1. Workplace — Anywhere Execution

Agents are not tied to a single machine. A **Workplace** is a first-class execution environment that can be:

- **Local** — sandboxed workspace on the host machine
- **Remote** — SSH servers, edge devices, or any machine with network access
- **Cloud** — lightweight Evonet connector that requires no public IP, no SSH, and no firewall rules

This means your agents can operate across your entire infrastructure — development laptops, production servers, and cloud instances — with a single abstraction layer.

### 2. Agent-to-Agent Communication

Communication between agents is a first-class protocol, not an afterthought. Agents can message, delegate, and coordinate with each other natively. This enables:

- **Multi-agent swarms** where each agent has a distinct role and toolset
- **Hierarchical orchestration** with supervisor agents managing worker agents
- **Peer-to-peer collaboration** for complex multi-step workflows

Each agent maintains its own identity, state, and capabilities, making swarm intelligence a natural pattern rather than a bolt-on feature.

### 3. Heuristic Mal-activity Detection System

Safety is not optional. Every action an agent takes is inspected through a real-time, multi-layer heuristic detection system that identifies and blocks dangerous patterns before execution. The system monitors for:

- Mass file deletion and privilege escalation attempts
- Unauthorized remote code execution
- Behavioral drift beyond expected action boundaries

When suspicious activity is detected, the system escalates to a human operator rather than blindly executing — giving you a safety net that enables genuine agent autonomy without compromising security.

---

## Key Features

| Feature | Description |
|---------|-------------|
| **Agents** | Independent, LLM-powered assistants with custom tools, knowledge bases, and isolated workspaces |
| **Models** | Pluggable LLM backends — any OpenAI-compatible API, local or cloud |
| **Skills** | Installable packages that bundle tool definitions with Python backends |
| **Plugins** | Event-driven extensions for custom integrations and background workers |
| **Workplaces** | Execution environments: local directories, SSH servers, or cloud devices via Evonet |
| **Evonet** | Lightweight Go connector for remote execution without SSH or firewall rules |
| **Scheduler** | Cron-based triggers, recurring tasks, and reminders for agents |
| **Channels** | Connect agents to Telegram, WhatsApp, Discord, Slack, and custom interfaces |
| **Evaluation Engine** | Automated LLM evaluation with customizable regex and heuristic evaluators |

---

## Getting Started

### Prerequisites

| | Linux / macOS | Windows |
|---|---|---|
| **Python** | 3.8+ | 3.8+ ([python.org](https://www.python.org/downloads/)) |
| **Git** | system package | [git-scm.com](https://git-scm.com/download/win) |
| **LLM endpoint** | any OpenAI-compatible API (local or cloud) | same |

### Installation

#### Linux / macOS

```bash
curl -fsSL https://evonic.dev/install.sh | bash
```

#### Windows (PowerShell)

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
irm https://evonic.dev/install.ps1 | iex
```

Both installers copy the source to `~/.evonic` (Linux/macOS) or `%USERPROFILE%\.evonic` (Windows), create a virtual environment, install dependencies, and add `evonic` to your PATH.

> **Windows tip:** Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first — the installer detects it automatically and uses it for significantly faster dependency installation.

See **[docs/windows-installation.md](docs/windows-installation.md)** for the full Windows guide including local dev setup and troubleshooting.

#### Manual (Linux / macOS)

```bash
git clone https://github.com/anvie/evonic ~/.evonic
cd ~/.evonic
pip install -r requirements.txt
chmod +x ./evonic
./evonic setup
./evonic start
```

#### Manual (Windows)

```powershell
git clone https://github.com/anvie/evonic "$env:USERPROFILE\.evonic"
cd "$env:USERPROFILE\.evonic"

# with uv (recommended)
uv venv venv --python python --seed
uv pip install -r requirements.txt --python venv

# or standard pip
# python -m venv venv
# .\venv\Scripts\pip install -r requirements.txt

.\evonic.bat setup
.\evonic.bat start
```

### Start

```bash
# Linux / macOS
./evonic start
```

```powershell
# Windows (from repo directory)
.\evonic.bat start

# Windows (from anywhere, after installer sets PATH)
evonic start
```

Open `http://localhost:8080` in your browser.

### Docker Sandbox (optional)

Agent tools like `bash` and `runpy` execute inside an isolated Docker container by default:

```bash
docker build -t evonic-sandbox:latest docker/tools/
```

Configure resource limits in `.env` (memory, CPU, network). If Docker is unavailable, set `sandbox_enabled=0` to fall back to local execution.

---

## Agents

Each agent is designed from the ground up with six configurable dimensions:

| Dimension | Description |
|-----------|-------------|
| **Concept** | System prompt and identity — who the agent is and how it behaves |
| **Model** | LLM backend — which model powers the agent's reasoning |
| **Tools** | Capabilities — what actions the agent can take |
| **Knowledge Base** | Reference documents — what information the agent can access |
| **Channels** | Interfaces — where users interact with the agent |
| **Skills** | Modular extensions — additional capabilities installed on demand |

Create and manage agents via the web UI (`/agents`) or CLI:

```bash
# Linux / macOS
./evonic agent add my_bot --name "My Bot"
./evonic agent add dev_bot --name "Dev Bot" --skillset coder
./evonic agent enable my_bot
./evonic agent remove my_bot
```

```powershell
# Windows
.\evonic.bat agent add my_bot --name "My Bot"
.\evonic.bat agent add dev_bot --name "Dev Bot" --skillset coder
```

---

## Channels

Connect your agents to the platforms your users already use:

| Channel | Status | Library |
|---------|--------|---------|
| **Telegram** | ✅ Implemented | `python-telegram-bot` |
| **WhatsApp** | ✅ Implemented | `@whiskeysockets/baileys` (Node.js sidecar) |
| **Discord** | 🔄 Planned | `discord.py` |
| **Slack** | 🔄 Planned | `slack-sdk` |

---

## Skills

Skills extend agents with new capabilities. Install via CLI:

```bash
./evonic skill install path/to/skill.zip
./evonic skill list
./evonic skill enable math
./evonic skill uninstall math
```

Skills follow a **load → context → execute** lifecycle, keeping the agent's system prompt lean and modular.

---

## Plugins

Plugins are event-driven extensions that hook into Evonic's event stream. Manage them via CLI:

```bash
./evonic plugin install path/to/plugin.zip
./evonic plugin list
./evonic plugin uninstall my_plugin
```

---

## Models

Manage LLM configurations:

```bash
./evonic model add gpt4o --name "GPT-4o" --provider openai --api-key "sk-..." --base-url "https://api.openai.com/v1"
./evonic model list
./evonic model rm gpt4o
```

---

## Use Cases

Evonic's architecture unlocks a broad spectrum of real-world applications. Here are fifteen concrete scenarios:

### Customer Service
Deploy agents that handle support tickets, answer FAQs, process refunds, and escalate complex issues — all within your existing Telegram or WhatsApp channels.

### Personal Companion
Build personal assistants that manage daily tasks, set reminders, conduct research, and maintain long-term context across conversations.

### Agentic Swarm / Multi-Agent Orchestration
Orchestrate multiple agents with distinct roles — a researcher, a writer, a reviewer, and a publisher — collaborating autonomously on complex deliverables.

### Automation & DevOps
Deploy agents that monitor server health, trigger deployments, roll back faulty releases, and respond to incidents with automated runbooks.

### Research Assistant
Create agents that perform literature reviews, extract structured data from documents, summarize findings, and generate citations.

### Customer Onboarding
Guide new users through product setup with interactive agents that adapt to each user's pace and knowledge level.

### Quality Assurance & Evaluation
Automate LLM evaluation pipelines — define test cases, run evaluations across models, and generate benchmark reports automatically.

### Internal Helpdesk
Provide IT support, HR policy lookups, and facility requests through a single agent interface connected to your internal knowledge base.

### E-commerce Assistant
Power product recommendations, order tracking, cancellation requests, and inventory inquiries — connected to your commerce backend.

### Healthcare Triage
Deploy agents that conduct initial symptom assessment, schedule appointments, and route critical cases to the appropriate specialist.

### Education Tutor
Build adaptive tutoring agents that personalize learning paths, grade assignments, and provide real-time feedback to students.

### Content Moderation
Scan user-generated content for harmful patterns, flag violations, and take appropriate action — all within configurable safety boundaries.

### Financial Advisory
Create agents that analyze portfolios, generate market summaries, assess risk profiles, and provide data-driven financial insights.

### Agentic ERP
Orchestrate enterprise resource planning workflows — supply chain monitoring, inventory optimization, procurement automation, and financial reconciliation — through specialized agents that coordinate across departments.

### AI Workflow Orchestration
Design end-to-end AI pipelines where agents manage the entire lifecycle: data ingestion, preprocessing, model training, evaluation, and deployment — with each stage handled by a specialized agent.

---

## Architecture Overview

```
User Message
    ↓
Channel (Telegram, Web, WhatsApp, etc.)
    ↓
Agent Runtime
    ├── Load agent config (system prompt, model, tools)
    ├── Load/create session (per-user persistence)
    ├── Build messages (system prompt + history + new message)
    ├── Call LLM
    ├── Execute tool calls (if any)
    ├── Heuristic safety check on every action
    └── Loop until final response
    ↓
Response → Channel → User
```

---

## License

Evonic is open source. See the [LICENSE](LICENSE) file for details.

---

*Built with ❤️ by [Robin Syihab](https://github.com/anvie)*
