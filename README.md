# teardrop
Intelligence beyond the browser

## What it is

Teardrop is a streaming AI agent API. You send it a message; it reasons using Claude, optionally calls tools, builds a UI component tree, and streams everything back as Server-Sent Events. It implements three open protocols simultaneously: **AG-UI** (streaming events), **A2A** (agent discoverability), and **MCP** (tool serving).

---

## Requirements

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/)

---

## Setup (PowerShell)

**1. Clone and enter the project**
```powershell
cd "C:\Users\<you>\Documents\Local Repositiories\teardrop"
```

**2. Create and activate a virtual environment**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

> If you get a script execution error, run first:
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```

**3. Install dependencies**
```powershell
pip install -r requirements.txt
```

**4. Configure environment**

Create a `.env` file in the project root:
```powershell
Copy-Item .env.example .env   # if it exists, otherwise create manually
```

Minimum required contents:
```
ANTHROPIC_API_KEY=sk-ant-...
```

Optional settings (all have defaults):
```
APP_ENV=development
APP_HOST=0.0.0.0
APP_PORT=8000
APP_LOG_LEVEL=info
CORS_ORIGINS=*
AGENT_MODEL=claude-3-5-sonnet-20241022
AGENT_MAX_TOKENS=4096
AGENT_TEMPERATURE=0.0
RATE_LIMIT_REQUESTS_PER_MINUTE=60
LANGSMITH_TRACING=false
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=teardrop
```

**5. Run the API server**
```powershell
uvicorn app:app --reload
```

Server starts at `http://localhost:8000`. Visit `http://localhost:8000/docs` for the interactive API explorer.

---

## Running the MCP tool server (optional)

The tools can be served standalone over the MCP protocol for use with Claude Desktop or other MCP clients:

```powershell
# stdio transport (default – for Claude Desktop)
python tools/mcp_server.py

# HTTP SSE transport
python tools/mcp_server.py --transport=sse
```

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Redirects to `/docs` |
| `GET` | `/health` | Liveness probe – returns service status and version |
| `POST` | `/agent/run` | Main streaming endpoint – send a message, receive SSE events |
| `GET` | `/.well-known/agent-card.json` | A2A agent card for inter-agent discoverability |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/redoc` | ReDoc UI |

### Calling the agent (PowerShell)
```powershell
$body = '{"message": "What is 42 * 7?", "thread_id": "my-session-1"}'
Invoke-RestMethod -Uri "http://localhost:8000/agent/run" `
                  -Method Post `
                  -ContentType "application/json" `
                  -Body $body
```

For multi-turn conversation, reuse the same `thread_id` across requests.

---

## How it works

### Agent graph (`agent/graph.py`)

The agent runs as a LangGraph state machine with three nodes:

```
START → planner → [tool_executor ↩] → ui_generator → END
```

- **planner** — Sends the conversation to Claude with all tools bound. If Claude decides to call a tool, status is set to `EXECUTING`; otherwise it moves to UI generation.
- **tool_executor** — Runs all pending tool calls in parallel, appends `ToolMessage` results, then loops back to the planner for further reasoning.
- **ui_generator** — Extracts or generates A2UI component JSON from the final assistant message and attaches it to the state.

Conversation history persists across turns via an in-memory `MemorySaver` checkpointer keyed by `thread_id`.

### Streaming (`app.py`)

`POST /agent/run` returns a live SSE stream. Event types emitted:

| Event | When |
|-------|------|
| `RUN_STARTED` | Immediately on request |
| `TEXT_MESSAGE_CONTENT` | Each LLM token chunk |
| `TOOL_CALL_START` | Before a tool executes |
| `TOOL_CALL_END` | After a tool returns |
| `SURFACE_UPDATE` | When A2UI components are ready |
| `RUN_FINISHED` | Agent completed normally |
| `ERROR` | Unhandled exception |
| `DONE` | Stream closed |

### Tools (`tools/mcp_tools.py`)

Four tools are available to the agent and the MCP server:

| Tool | Description |
|------|-------------|
| `calculate` | Evaluates arithmetic expressions safely (no `eval`). Supports `+`, `-`, `*`, `/`, `**`, `%`, `sqrt`, `abs`, `round`, `floor`, `ceil`, `log`, `sin`, `cos`, `tan`, `pi`, `e`. |
| `get_datetime` | Returns current UTC date/time. Accepts an optional `strftime` format string. |
| `web_search` | **Stub** — returns a placeholder result. Wire in `SERPER_API_KEY` or `TAVILY_API_KEY` to activate. |
| `summarize_text` | Returns character count, word count, sentence count, paragraph count, and average words per sentence for a given text. |

### A2UI components (`agent/state.py`, `agent/nodes.py`)

The agent can return structured UI alongside text. Supported component types:

| Type | Props |
|------|-------|
| `text` | `content`, `variant` (`body`\|`heading`\|`caption`) |
| `table` | `columns`, `rows` |
| `columns` | `children` |
| `rows` | `children` |
| `form` | `fields`, `submit_label` |
| `button` | `label`, `action` |
| `progress` | `value` (0–100), `label` |

---

## Project structure

```
app.py              # FastAPI app, SSE streaming, rate limiting
config.py           # Settings loaded from .env via pydantic-settings
agent/
  graph.py          # LangGraph StateGraph definition and routing logic
  nodes.py          # planner, tool_executor, ui_generator node implementations
  state.py          # AgentState, A2UIComponent, TaskStatus schemas
tools/
  mcp_tools.py      # Tool implementations + LangChain StructuredTool wrappers
  mcp_server.py     # Standalone FastMCP server for MCP protocol clients
  __init__.py       # Re-exports get_langchain_tools()
```
