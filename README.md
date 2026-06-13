# simcpi

Building MCP tools today means maintaining two separate things — an API for your app and an MCP server for your AI client. simcpi collapses that into a single decorator, then gives you a browser tool to **test, rate, and improve** the result.

Define a function once. Get a REST endpoint, an MCP tool, and a visual tester — all at the same time. Then use MCPark to watch a real LLM call your tools, rate the results, and have simcpi rewrite your tool descriptions from that feedback.

**Features:**
- `@create_tool_api` — one decorator registers both a FastAPI route and an MCP tool
- **MCPark** — built-in browser UI to inspect, LLM-test, rate, and optimize MCP tools
- **Feedback memory** — log prompt → tool → 👍/👎 (+ optional reason), retrieve similar rated examples as few-shot hints on future runs
- **Docstring optimizer** — rewrite tool descriptions and score them against your ratings, then apply live or write straight back into your source
- **Remote inspector** — point MCPark (or the `mcpark` CLI) at *any* MCP server, not just simcpi ones
- `serve_file()` — return DataFrames, charts, images, or any file from an MCP tool in one line
- `QuickClient` / `MCPClient` — connect to any MCP server and run tools through an LLM
- `connect_claude()` / `connect_cursor()` — auto-register your server in Claude Desktop or Cursor
- **Optional bearer-token auth** — one `auth_token=` argument protects every route
- Multi-server support — combine tools from multiple MCP servers in one client

**How it works:**
simcpi wraps FastAPI and FastMCP behind a single `MCPApi` class. When you decorate a function with `@app.create_tool_api()`, it registers the function as a FastAPI route (accessible via REST) and as a FastMCP tool (accessible via the MCP streamable-HTTP transport) simultaneously. The same server handles both protocols. MCPark is mounted automatically and connects to the live MCP transport to test tools interactively.

```bash
pip install simcpi
```

---

## Quick Start

```python
from simcpi import MCPApi
import uvicorn

app = MCPApi(
    title="my-api",
    provider="openai",
    api_key="sk-...",
    model="gpt-4o",
)

@app.create_tool_api("/greet")
def greet(name: str) -> str:
    """
    Greet the user by name.
    Use this when the user wants to say hello.
    """
    return f"Hello, {name}!"

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

- REST endpoint: `POST /greet`
- MCP tool: `greet`
- Visual tester: `http://localhost:8000/mcpark`
- Swagger UI: `http://localhost:8000/docs`

---

## Features

### 1. `@create_tool_api` — REST + MCP in one decorator

```python
@app.create_tool_api("/add")
def add(a: int, b: int) -> int:
    """Add two numbers. Use when the user asks to sum or total values."""
    return a + b
```

- Creates `POST /add` (FastAPI route)
- Registers `add` as an MCP tool (FastMCP)
- Docstring is the tool description the LLM sees — write it for the model
- Async functions work. `Depends()` parameters are automatically stripped from the MCP tool schema (the LLM never sees them; they're `None` on MCP calls, injected normally on REST calls)

---

### 2. MCPark — visual MCP testing UI

Open `http://localhost:8000/mcpark` in any browser.

- Lists all registered tools in the sidebar
- Run tools with an LLM in the loop — select provider, paste API key
- **Assistant toggle** — off gives raw tool output, on gives LLM interpretation
- File results render inline: images show as images, Excel as a table, PDFs open in-page
- MCP image content (e.g. a chart returned directly by a tool) renders inline too
- **✨ Generate Test Cases** — auto-generates single-tool and multi-tool test prompts for every tool
- **Connect to any MCP server** — the MCP Server field accepts any URL + optional bearer token, not just this server (see *Remote inspector* below)
- One-click copy of the Claude Desktop config snippet

No separate install. No external service. Ships inside the package.

---

### 3. Feedback memory — learn which tool the LLM should pick

Opt-in, default off. Turn it on and MCPark starts logging every run as `prompt → tool(s) + params → rating`, then uses that history to steer future runs.

```python
app = MCPApi(
    title="my-api",
    provider="openai",
    api_key="sk-...",
    feedback=True,                 # turn it on
)
```

In MCPark, each run now shows a **👍 / 👎** block with an optional **"why?"** note (e.g. *"wanted Telugu, got Hindi"*). On the next run, simcpi embeds the new prompt, finds similar past prompts you rated, and injects them as few-shot hints — *"a similar request got a 👍 with tool X; a 👎 with tool Y."*

- **🧠 Recalled from feedback** block shows which past examples informed a run, with similarity %
- **🗄 View Feedback DB** button — browse the raw SQLite table (time, prompt, tool calls, rating, reason, embedded)
- **🗑 Clear DB** — wipe and start fresh (handy after improving docstrings; localhost-only)
- Stored in a local SQLite file (`./simcpi_feedback.db` by default), one vector per row — no external vector DB

Retrieval needs an OpenAI-compatible **embeddings** key. If none is available it degrades gracefully to log-and-rate (no retrieval). See the embedding parameters in *MCPApi parameters*.

---

### 4. Docstring optimizer — rewrite tool descriptions from your ratings

Once you've rated some runs, click **📝 Improve Docstrings** in MCPark. For each tool, simcpi:

1. Turns your rated runs into expectations (this prompt *should* / *should not* trigger this tool, plus your written reasons as the strongest signal),
2. Replays them through a selection-only LLM call to score the current description (tools are never executed),
3. Rewrites the description and re-scores it,
4. Shows `current% → candidate%` and keeps the rewrite only if it scored better.

Each improved result gives you three actions:

- **📋 Copy** — copy the new docstring to paste in yourself
- **⚡ Apply (live)** — update the running tool's description immediately (lasts until restart)
- **💾 Write to source** — rewrite the docstring straight into your `.py` file (AST-located, body untouched) *and* update it live. **Localhost-only** — an exposed MCPark cannot edit your source.

---

### 5. Remote inspector — MCPark for *any* MCP server

MCPark talks the MCP protocol over HTTP, so it works against servers you didn't build with simcpi.

```bash
# installed as a console command — point it at any MCP server
mcpark https://mcp.deepwiki.com/mcp
mcpark http://127.0.0.1:9000/mcp/mcp --auth my-token --port 8123

# equivalently
python -m simcpi https://mcp.deepwiki.com/mcp
```

Flags: `--auth` (bearer token for the target), `--port` (local UI port, default 8123), `--provider`, `--api-key`, `--model`, `--base-url`. With no URL it opens an empty MCPark you can connect from the UI.

Against a remote server you get explore + LLM-test + feedback memory + optimizer scoring + Copy. (Apply-live and Write-to-source need the target to be in-process, so they only apply to your own simcpi server.)

---

### 6. `serve_file()` — return files from MCP tools

MCP tools can only return text. `serve_file()` bridges that gap — pass your object directly and get back a URL the client can download or MCPark can preview.

```python
@app.create_tool_api("/report")
def sales_report() -> str:
    """Generate a sales report. Use when the user asks for sales data."""
    import pandas as pd
    df = pd.DataFrame({"Month": ["Jan","Feb","Mar"], "Sales": [1200, 1500, 1800]})
    return app.serve_file("report.xlsx", df, port=8000)

@app.create_tool_api("/chart")
def sales_chart() -> str:
    """Generate a sales chart."""
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots()
    ax.bar(["Jan","Feb","Mar"], [1200, 1500, 1800])
    return app.serve_file("chart.png", fig, port=8000)

@app.create_tool_api("/image")
def generate_image(prompt: str) -> str:
    """Generate an image from a prompt."""
    url = call_image_api(prompt)             # returns a remote image URL
    return app.serve_file("image.png", url, port=8000)
```

| Content type | What happens |
|---|---|
| `pandas.DataFrame` | Serialized to `.xlsx`, `.csv`, or `.json` based on filename |
| `matplotlib.Figure` | Rendered to `.png` or `.pdf` |
| `bytes` | Written directly |
| `http://...` / `https://...` | Downloaded from URL and served |
| `str` / `Path` | Treated as a local file path, served as-is |

Served files are tracked with a **TTL and a size cap** so a long-running server doesn't leak memory or temp files — links expire after `file_ttl` seconds (default 3600) and the store holds at most `file_store_max` entries (default 256), evicting the oldest. Temp copies simcpi created are deleted on eviction; file paths you passed in are never touched.

Install extras for DataFrame and Figure support:
```bash
pip install simcpi[files]
```

---

### 7. Auth — one argument protects everything

Opt-in, default off. Set `auth_token` and every route (REST, MCP, MCPark, `/files`) requires it.

```python
app = MCPApi(title="my-api", auth_token="my-secret-token")
```

Accepted credentials:
- `Authorization: Bearer my-secret-token` — MCP clients, curl, scripts
- `?key=my-secret-token` — first browser visit; plants a cookie so MCPark keeps working
- `simcpi_key` cookie — subsequent browser requests

The clients and config helpers carry it for you:

```python
QuickClient("http://localhost:8000/mcp/mcp", provider="openai", api_key="sk-...",
            auth_token="my-secret-token")

app.connect_claude(port=8000)   # writes the bearer header into the Claude config automatically
```

For custom schemes (JWT, OAuth) you can still add your own `BaseHTTPMiddleware` to `app.api` — it runs on all paths including `/mcp/mcp`.

---

### 8. `QuickClient` — sync MCP client in 5 lines

```python
from simcpi import QuickClient

client = QuickClient(
    mcp_server="http://localhost:8000/mcp/mcp",
    provider="openai",
    api_key="sk-...",
    model="gpt-4o",
    auth_token="my-secret-token",   # only if the server uses auth
)

print(client.run("Greet Mohan in Telugu"))
```

File results are auto-detected and downloaded:

```python
result = client.run("Give me the sales report")
print(result.save())   # saves to disk, prints the path
```

Multi-server — tools from all servers combined:

```python
client = QuickClient(
    mcp_server=[
        "http://localhost:8000/mcp/mcp",
        "http://localhost:8080/mcp/mcp",
    ],
    ...
)
```

List available tools:

```python
client.tools()
# {"http://localhost:8000/mcp/mcp": ["greet", "report"], ...}
```

---

### 9. `MCPClient` — async client with full trace

```python
import asyncio
from simcpi import MCPClient

client = MCPClient(
    mcp_server="http://localhost:8000/mcp/mcp",
    provider="anthropic",
    api_key="sk-ant-...",
    model="claude-sonnet-4-20250514",
    auth_token="my-secret-token",   # only if the server uses auth
)

result = asyncio.run(client.run("Add 42 and 58"))
print(result.answer)        # "100"
print(result.tools_called)  # [ToolCall(name="add", arguments={...}, result="100")]
print(result.success)       # True
```

| Field | Type | Description |
|---|---|---|
| `answer` | `str` | Final LLM response |
| `tools_called` | `list[ToolCall]` | Every tool invoked, in order |
| `success` | `bool` | False if an exception occurred |
| `error` | `str \| None` | Error message if `success=False` |

---

### 10. `connect_claude()` — Claude Desktop integration

**On the server instance:**

```python
app.connect_claude(port=8000)                            # local server
app.connect_claude(url="https://myapi.com/mcp/mcp")     # remote server
```

**Standalone — no MCPApi needed (register multiple servers at once):**

```python
from simcpi import connect_claude

connect_claude("my-api",   port=8000)
connect_claude("reports",  port=8080)
connect_claude("prod",     url="https://myapi.com/mcp/mcp", auth_token="secret")
```

Multiple servers append as separate entries — no collision. If the server uses auth, the bearer header is written into the config automatically. Restart Claude Desktop after calling.

---

### 11. `connect_cursor()` — Cursor IDE integration

```python
app.connect_cursor(port=8000)                        # global (~/.cursor/mcp.json)
app.connect_cursor(port=8000, scope="project")       # project (.cursor/mcp.json)

# standalone
from simcpi import connect_cursor
connect_cursor("my-api", port=8000)
connect_cursor("prod",   url="https://myapi.com/mcp/mcp", auth_token="secret")
```

---

### 12. `get_client()` — in-process client

When you're already inside the server process, skip the HTTP round-trip:

```python
client = app.get_client()
result = await client.run("Add 42 and 58")
```

---

### 13. `export_mcpb()` — one-click Claude Desktop bundle

```python
app.export_mcpb()   # writes My API.mcpb — double-click to install in Claude Desktop
```

---

## MCPApi parameters

```python
app = MCPApi(
    title="my-api",          # API name — used as key in Claude/Cursor config
    provider="openai",       # "openai" or "anthropic" — pre-fills MCPark UI
    api_key="sk-...",        # pre-fills MCPark UI, used by get_client()
    base_url="https://...",  # custom LLM endpoint (AICredits, OpenRouter, etc.)
    model="gpt-4o",          # pre-fills model in MCPark UI
    description="...",       # shown in Swagger + MCP server instructions
    mcp_path="/mcp",         # MCP transport prefix (default: /mcp)
    mcpark_path="/mcpark",   # MCPark UI path, set None to disable
    version="1.0.0",

    # auth (default off)
    auth_token=None,         # set a string → every route requires Bearer/?key=/cookie

    # served-file lifecycle
    file_ttl=3600,           # seconds a serve_file() link stays valid (None = never)
    file_store_max=256,      # max live served files; oldest evicted past this

    # feedback memory (default off)
    feedback=False,          # turn on the log → rate → retrieve loop in MCPark
    feedback_db=None,        # SQLite path (default ./simcpi_feedback.db)
    feedback_only_positive=False,  # retrieve only 👍 examples
    feedback_k=5,            # max neighbours retrieved per prompt
    feedback_threshold=0.6,  # min cosine similarity to count as a neighbour

    # embeddings (used by feedback retrieval)
    embedding_model="text-embedding-3-small",  # fixed per DB — see Embeddings note below
    embedding_api_key=None,  # separate embeddings key (else reuses api_key if OpenAI)
    embedding_base_url=None,
)
```

---

## Endpoints created automatically

| Path | Description |
|---|---|
| `GET /docs` | Swagger UI |
| `GET /mcpark` | MCPark visual testing UI |
| `POST /mcp/mcp` | MCP streamable-HTTP transport |
| `GET /files/{token}/{filename}` | File serving for `serve_file()` |

MCPark also mounts internal routes it drives itself: `/mcpark/tools`, `/mcpark/run`, `/mcpark/generate-tests`, `/mcpark/rate`, `/mcpark/feedback-stats`, `/mcpark/feedback-rows`, `/mcpark/feedback-clear` (localhost-only), `/mcpark/embed-backfill`, `/mcpark/optimize`, `/mcpark/apply-docstring`, and `/mcpark/write-docstring` (localhost-only).

---

## Supported providers

Any OpenAI-compatible provider works via `base_url`:

| Provider | Notes |
|---|---|
| OpenAI | `model="gpt-4o"` |
| Anthropic | `provider="anthropic"`, `model="claude-sonnet-4-20250514"` |
| OpenRouter | custom `base_url` |
| AICredits | custom `base_url` |
| Any OpenAI-compatible API | custom `base_url` |

> **Embeddings note.** Feedback-memory **retrieval** always goes through an OpenAI-style embeddings endpoint — `embedding_model` must be servable that way (e.g. `text-embedding-3-small` / `-large`, not an Anthropic model). With an Anthropic chat key you can still log and rate; for retrieval supply a separate `embedding_api_key`.
>
> **Pick one `embedding_model` and keep it.** Every stored vector must share one embedding space, so the model is fixed per database — there's no per-run override. If you switch models after collecting data, old vectors stop matching: a *different-dimension* model leaves them safely ignored (backfill only fills **missing** embeddings, it never re-embeds), and a *same-dimension different* model silently mixes spaces and quietly degrades retrieval. Changing models? Click **🗑 Clear DB** (or re-embed) first.

---

## Requirements

- Python 3.10+
- fastapi, fastmcp, pydantic, uvicorn, openai, anthropic

> **Note:** simcpi works with `fastmcp>=3.2` (verified through 3.4.x). If `pip install -U fastmcp` ever leaves you with `ImportError: cannot import name 'FastMCP'`, the upgrade got clobbered by a pip ordering quirk — fix it with `pip install --force-reinstall "fastmcp-slim[client,server]"`.

Optional (`pip install simcpi[files]`):
- pandas + openpyxl — Excel / CSV output from DataFrames
- matplotlib — PNG / PDF output from Figures

---

## License

MIT
