# AI Workflow Builder

AI Workflow Builder is a local, visual orchestrator for AI-assisted software development. Drag Codex,
Claude Code, opencode, pi, Shell, Condition, and Git nodes onto a canvas, connect them, and turn them
into repeatable, observable, version-controlled workflows.

This is not another pipeline script with hard-coded steps. A workflow is JSON data stored in the
target repository and tracked by Git. Every run happens in isolated worktrees and task branches, so
the working directory you currently have checked out is never modified directly.

## What It Does

- Build planning, implementation, testing, review, and retry flows with a visual editor.
- Manage multiple Git repositories from one service, with separate workflows and run history for
  every project.
- Stream node status, agent messages, tool calls, file changes, test output, and token usage in real
  time.
- Support fan-out, fan-in, conditional branches, retry loops, and `all` / `any` joins.
- Pass typed JSON between QA nodes instead of parsing arbitrary prose for success or failure.
- Persist events in SQLite, replay them after a page refresh, and resume SSE streams after a
  disconnect.
- Provide `shared` and `per_node` Git isolation modes.
- Add new agent CLIs with a YAML adapter and Python normalizer, without changing the scheduler.

A typical workflow looks like this:

```text
Requirement → Codex Plan → Claude Implement → Git Commit → Tests → Codex QA → Passed?
                                ↑                                      │ false
                                └──────────────────────────────────────┘
                                                                       │ true
                                                                      Done
```

## Quick Start

### 1. Install

Requirements:

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- At least one agent CLI you intend to use, such as `codex`, `claude`, `opencode`, or `pi`
- A Git repository with at least one commit

```bash
uv venv
uv pip install -r requirements.txt
```

### 2. Check the Environment

```bash
.venv/bin/python -m tools.doctor
```

The doctor checks whether agent CLIs can run, whether the configured Codex model is compatible with
the installed CLI version, and whether registered projects are currently usable.

### 3. Start the Service

```bash
.venv/bin/python app.py
```

Open [http://localhost:5111](http://localhost:5111). Use the project manager in the top-right corner
to add a Git repository. The service initializes `.ai-workflow-proj/` inside that repository. You can
then create a workflow or import one of the bundled templates.

If an agent needs an API key, export it in the **same shell that starts the service**. Agent
subprocesses inherit the environment captured by the Flask process at startup. `config.local.yaml`
does not load `.env` files automatically.

## Basic Workflow

1. Register a target Git repository.
2. Create a workflow or import a bundled template.
3. Drag nodes onto the canvas, configure prompts and parameters, and connect their execution order.
4. Save and run the workflow.
5. Watch node status, events, changes, and artifacts on the run page.
6. Inspect `task/<run-id>` and decide whether to merge it:

```bash
git merge --no-ff task/<run-id>
```

The system never merges results automatically and never modifies your currently checked-out working
directory directly.

## Project Data Layout

Registering a project creates this structure:

```text
<project>/.ai-workflow-proj/
├── project.yaml              # Tracked: project name, base branch, isolation, guard overrides
├── workflows/
│   └── *.json                # Tracked: workflow definitions; filename is the workflow ID
├── .gitignore                # Tracked: excludes local/
├── local.yaml                # Untracked: optional machine-specific overrides
└── local/                    # Untracked
    ├── ai-workflow.sqlite    # Run, node, and event history
    └── runs/<run-id>/
        └── artifacts/        # Schemas, structured output, and other run artifacts
```

The boundary is intentional:

- **Workflow definitions belong to the project**, so they are reviewable and shareable JSON files.
- **Run history belongs to the local machine**, so it stays under `local/` and outside Git.
- Worktrees live under the global `worktree_root/<project-id>/<run-id>/`, not inside the target
  repository.

After a teammate clones the repository, registering its path in their own AI Workflow Builder gives
them the same `project.yaml` and workflows. Each teammate keeps independent run history and local
paths.

### Configuration Precedence

Settings are merged from lowest to highest priority:

1. `config.yaml` — tool defaults.
2. `config.local.yaml` — machine-wide overrides, not tracked by Git.
3. `<project>/.ai-workflow-proj/project.yaml` — shared project settings, tracked by Git.
4. `<project>/.ai-workflow-proj/local.yaml` — project-specific machine overrides, not tracked.
5. Workflow `settings` — currently overrides `isolation` and `max_run_steps`.

Project settings may override `main_branch`, `isolation`, and `guards`. Put `worktree_root` in
`local.yaml` because it describes a machine-specific path. The run database and artifacts always
remain under `.ai-workflow-proj/local/`.

## Node Types

| Node | Purpose | Main outputs |
|---|---|---|
| Requirement | Starts a run and optionally uses the requirement entered at launch | `requirement`, `last_message` |
| Codex | Planning, implementation, and review with JSON Schema support | `last_message`, `structured`, `session_id`, `files`, `usage` |
| Claude Code | Agent implementation and multi-file changes with JSON Schema support | Same as above |
| opencode | Runs a selected provider, model, or custom agent | Same as above, without schema support |
| pi | Can be restricted to read-only tools such as `read` and `grep` | Same as above, without schema support |
| Shell | Runs tests, lint, builds, or arbitrary commands | `stdout`, `exit_code` |
| Condition | Safely evaluates an expression and selects the `true` or `false` output | Boolean result |
| Git | Commits current changes or refreshes the diff | `sha`, `diff` |

Every node can also configure:

- `mutates` — whether it changes the workspace; affects parallel scheduling.
- `max_visits` — maximum executions during one run.
- `timeout_sec` — node timeout.
- `join` — wait for `all` incoming edges or any one of them.
- `on_error` — abort the run or continue so a downstream condition can handle the failure.

## Prompts and Conditions

Prompts, Shell commands, and selected node fields support Jinja templates:

```jinja
Implement the following plan. This is iteration {{ loop.iteration }}:

{{ nodes.plan.last_message }}

{% if nodes.qa.structured %}
Issues from the previous QA pass:
{% for issue in nodes.qa.structured.issues %}
- {{ issue }}
{% endfor %}
{% endif %}
```

Common variables include:

- `requirement`
- `nodes.<id>.last_message`
- `nodes.<id>.structured`
- `nodes.<id>.exit_code`
- `nodes.<id>.files`
- `run.id`, `run.repo`, `run.branch`
- `run.diff`, `run.changed_files`
- `loop.iteration`

Conditions use an allow-listed expression evaluator, not Python `eval`:

```text
nodes.qa.structured.verdict == 'PASS' and nodes.tests.exit_code == 0
```

To create a retry loop, connect a Condition node's `false` output back to the upstream node that
should run again. Three limits prevent infinite loops: node-level `max_visits`, run-level
`max_run_steps`, and the overall run timeout.

## Isolation Modes

Set the isolation mode with `settings.isolation` in a workflow.

### `shared` (Default)

The entire run shares one worktree and one `task/<run-id>` branch.

- Nodes can continue from workspace state left by the previous node.
- Read-only nodes can execute in parallel.
- Nodes with `mutates=true` share a write lock and therefore execute serially.
- Any remaining changes are committed to the task branch when a successful run finishes.
- The model is simple and works well for linear workflows and ordinary QA retry loops.

### `per_node`

Every node receives its own worktree and `node/<run-id>/<node-id>` branch.

- Mutating nodes can execute in parallel.
- A node must commit automatically before its state can be passed downstream.
- Fan-in merges upstream commits. A conflict fails the run explicitly and reports the conflicting
  files.
- At the end of a run, all final results are integrated so `task/<run-id>` represents the complete
  output.
- Disk usage is higher, approximately the number of node worktrees multiplied by repository size.

Use `shared` for mostly linear collaboration. Choose `per_node` only when mutating nodes truly need
to work concurrently, and keep parallel branches responsible for different files or modules.

> Git worktrees isolate files and branches, but they are not an operating-system security sandbox.
> Configure agent permissions, available tools, and credentials according to the principle of least
> privilege.

## Bundled Templates

| ID | Purpose |
|---|---|
| `sample-project-notes` | Recommended first run; analyzes a repository and creates `PROJECT_NOTES.md` |
| `sample-opencode-pi` | opencode implements and pi performs a read-only review, retrying on failure |
| `plan-impl-qa` | Codex plans, Claude implements, tests run, and Codex QA drives a repair loop |
| `codex-review` | Codex reviews the current branch and Claude fixes findings before another review |

Import templates from the UI when adding a project, or use the command line:

```bash
# List templates and registered projects
.venv/bin/python -m tools.templates

# List workflows in a project
.venv/bin/python -m tools.templates --project <project-id>

# Import one template or all templates
.venv/bin/python -m tools.templates --project <project-id> --import plan-impl-qa
.venv/bin/python -m tools.templates --project <project-id> --import all
```

Importing copies files without overwriting workflows with the same ID. The copied JSON becomes part
of the target project and should be committed and maintained there.

## Agent CLIs and Credentials

| Adapter | Executable | Structured output | Notes |
|---|---|---:|---|
| Codex | `codex` | Yes | Prefer the `read-only` sandbox for planning and review nodes |
| Claude Code | `claude` | Yes | Unattended Shell operations require an appropriate permission mode |
| opencode | `opencode` | No | Models usually use the `provider/model` format |
| pi | `pi` | No | Configure provider credentials first; a tool allowlist can make it read-only |
| Shell | `bash` | No | Executes inside the node worktree |

Run `tools.doctor` before starting a workflow. In particular:

- Agent CLIs must already be authenticated or have provider credentials configured.
- An older Codex CLI may not support a newer model selected in `~/.codex/config.toml`.
- pi is often installed in a Node or Bun directory that is not on `PATH`; the adapter includes
  common candidate paths.
- Git worktrees do not contain ignored directories such as `.venv` or `node_modules`. Test nodes
  must use an environment available from the target repository or reference dependencies explicitly.

## Adding an Adapter

Integrating a new agent CLI usually requires two files:

```text
adapters/<id>.yaml
adapters/normalizers/<id>.py
```

The YAML file defines the executable, arguments, working directory, prompt delivery, UI fields, and
capabilities. The normalizer turns real CLI output into the common event format.

Capture real output before implementing the normalizer:

```bash
.venv/bin/python -m tools.probe <adapter-id>
.venv/bin/python -m pytest tests/test_normalizers.py -q
```

Fixture provenance and regeneration instructions are documented in `tests/fixtures/README.md`. If a
CLI is not on `PATH`, list candidate locations under `binary_candidates` in its adapter YAML.

## API Overview

Workflows and runs are scoped to a project:

```text
GET    /api/projects
POST   /api/projects
DELETE /api/projects/<project-id>

GET    /api/projects/<project-id>/workflows
GET    /api/projects/<project-id>/workflows/<workflow-id>
POST   /api/projects/<project-id>/workflows
DELETE /api/projects/<project-id>/workflows/<workflow-id>
POST   /api/projects/<project-id>/workflows/import/<template-id>

POST   /api/projects/<project-id>/runs
GET    /api/projects/<project-id>/runs
GET    /api/projects/<project-id>/runs/<run-id>
GET    /api/projects/<project-id>/runs/<run-id>/diff
GET    /api/projects/<project-id>/runs/<run-id>/artifacts
GET    /api/projects/<project-id>/runs/<run-id>/events
POST   /api/projects/<project-id>/runs/<run-id>/cancel

GET    /api/runs
GET    /api/templates
POST   /api/workflows/validate
```

The `/events` endpoint uses SSE. Events are persisted to SQLite first, so clients can recover missed
events with `Last-Event-ID` or the `after` query parameter.

## Development and Testing

```bash
# Run the complete suite; tests do not call paid LLMs
.venv/bin/python -m pytest -q

# Capture the real event format of an agent CLI
.venv/bin/python -m tools.probe <adapter-id>

# Check the environment, CLIs, and registered projects
.venv/bin/python -m tools.doctor
```

Engine tests use the mock adapter but still exercise the same subprocess, line streaming, event
normalization, timeout, and cancellation paths used by real agents.

## Repository Structure

```text
app.py                       Flask pages, REST API, and SSE
settings.py                  Tool-level configuration loading and merging
engine/
  project.py                 Project initialization and .ai-workflow-proj settings
  graph.py                   Graph parsing, validation, cycles, and join rules
  runner.py                  Node scheduling, branches, loops, timeouts, and cancellation
  isolation.py               shared and per_node execution strategies
  executor.py                Agent subprocesses and streamed event collection
  context.py                 Jinja context and safe condition evaluation
  workspace.py               Git worktrees, branches, diffs, commits, and merges
  bus.py                     SQLite event persistence and SSE subscriber fan-out
  service.py                 Background run lifecycle and finalization
store/
  projects.py                Central project registry
  workflows.py               Workflow JSON storage inside each project
  stores.py                  Per-project Store management and cross-project run aggregation
  db.py                      SQLite access for runs, node runs, and events
adapters/                    CLI specifications and normalizers
static/js/
  project.js                 Project selection and management
  editor.js                  Workflow editor
  graph.js                   Drawflow-to-engine graph conversion
  run.js                     Live run view
workflows/                   Bundled templates available for import
tools/                       doctor, probe, templates, and watch utilities
tests/                       Engine, API, isolation, adapter, and storage tests
```

The engine accepts only its normalized graph format. `static/js/graph.js` is the only module that
understands Drawflow's data structure. Replacing the canvas library should therefore require a new
conversion layer, not a rewrite of the backend engine.

## Current Limitations

- Runs execute in Flask background threads. If the service restarts, unfinished runs are marked as
  failed rather than resumed.
- `shared` mode cannot run two mutating nodes concurrently.
- `per_node` mode may produce Git merge conflicts and consumes more disk space.
- Ignored dependency directories do not automatically appear in worktrees.
- The application is currently designed as a local tool and has no multi-user authentication or
  remote authorization model.
