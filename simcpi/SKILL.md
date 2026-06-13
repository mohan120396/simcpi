---
name: simcpi
description: >
  Build MCP servers using simcpi — a Python library that exposes any Python function
  as both a FastAPI REST endpoint and a FastMCP tool simultaneously with a single
  @create_tool_api decorator, plus MCPark: a browser UI to test, rate, and optimize
  MCP tools. Use this skill whenever the user is working with simcpi, wants to build
  MCP tools, asks about @create_tool_api, MCPApi, MCPark, serve_file(), connect_claude(),
  connect_cursor(), QuickClient, MCPClient, feedback memory, the docstring optimizer
  ("Improve Docstrings"), bearer-token auth (auth_token), the remote inspector / mcpark
  CLI, or any simcpi feature. Also trigger when the user wants to expose Python functions
  as both REST endpoints and MCP tools at the same time, combine FastAPI and FastMCP in
  one server, inspect/test any MCP server in the browser, or build tools for Claude
  Desktop, Cursor, or any MCP-compatible client.
---

# simcpi — MCP + REST from one decorator

simcpi wraps FastAPI and FastMCP behind a single class. One decorated function becomes
a REST endpoint (callable by any HTTP client) and an MCP tool (callable by any LLM
client like Claude Desktop or Cursor) simultaneously — same port, same process.

```python
from simcpi import MCPApi
import uvicorn

app = MCPApi(title="my-api", provider="openai", api_key="sk-...", model="gpt-4o")

@app.create_tool_api("/greet")
def greet(name: str) -> str:
    """
    Greet the user by name.
    Use this when the user wants to say hello to someone.
    """
    return f"Hello, {name}!"

uvicorn.run(app, host="127.0.0.1", port=8000)
```

This registers:
- `POST /greet` — FastAPI REST route
- `greet` — FastMCP tool the LLM can call
- `GET /mcpark` — visual tester UI
- `GET /docs` — Swagger UI
- `POST /mcp/mcp` — MCP streamable-HTTP transport

---

## MCPApi parameters

```python
app = MCPApi(
    title="my-api",           # server name, key in Claude/Cursor config
    provider="openai",        # "openai" or "anthropic" — pre-fills MCPark
    api_key="sk-...",         # pre-fills MCPark, used by get_client()
    base_url="https://...",   # custom LLM endpoint (AICredits, OpenRouter, etc.)
    model="gpt-4o",           # pre-fills MCPark model selector
    description="...",        # shown in Swagger + MCP server instructions
    mcp_path="/mcp",          # MCP transport prefix (default: /mcp)
    mcpark_path="/mcpark",    # MCPark path, set None to disable
    version="1.0.0",

    auth_token=None,          # set a string → every route requires Bearer/?key=/cookie

    file_ttl=3600,            # seconds a serve_file() link stays valid (None = never)
    file_store_max=256,       # max live served files; oldest evicted past this

    feedback=False,           # turn on MCPark's log → rate → retrieve loop
    feedback_db=None,         # SQLite path (default ./simcpi_feedback.db)
    feedback_only_positive=False,  # retrieve only 👍 examples
    feedback_k=5,             # max neighbours retrieved per prompt
    feedback_threshold=0.6,   # min cosine similarity to count as a neighbour
    embedding_model="text-embedding-3-small",   # fixed per DB — don't switch after collecting data
    embedding_api_key=None,   # separate embeddings key (else reuses api_key if OpenAI)
    embedding_base_url=None,
)
```

---

## @create_tool_api

```python
@app.create_tool_api(
    path="/endpoint",
    method="POST",            # GET, POST, PUT, PATCH, DELETE (default: POST)
    tool_name="custom_name",  # override MCP tool name (default: function name)
    tags=["category"],        # FastAPI Swagger tags
    summary="Short label",    # FastAPI summary
)
def my_tool(param1: str, param2: int) -> str:
    """
    This docstring is the MCP tool description the LLM reads.
    Write it for the model — explain WHEN to use this tool,
    not just what it does. The LLM uses this to decide whether
    to call the tool for a given user request.
    """
    return f"{param1} {param2}"
```

**Critical:** The docstring is read by the LLM to decide when to call the tool.
Bad: `"Returns the sum of a and b."` 
Good: `"Add two numbers. Use this when the user asks to sum, total, or combine values."`

**Async tools work too:**
```python
@app.create_tool_api("/fetch-data")
async def fetch_data(url: str) -> str:
    """Fetch data from a URL."""
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        return r.text
```

---

## FastAPI Depends() — automatic stripping

`Depends()` parameters are automatically stripped from the MCP tool schema.
The LLM never sees them. FastAPI still injects them normally on REST calls.
On MCP calls they are `None` — use middleware for MCP auth instead.

```python
from fastapi import Depends
from fastapi.security import HTTPBearer

security = HTTPBearer()

def verify_token(credentials=Depends(security)):
    # validates JWT, returns user payload
    return jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"])

@app.create_tool_api("/greet")
def greet(name: str, user=Depends(verify_token)) -> str:
    """Greet the user."""
    # user is None on MCP calls (auth handled by middleware)
    # user is the JWT payload on REST calls
    return f"Hello {name}"
```

For MCP auth, add middleware to `app.api` — it runs on all paths including `/mcp/mcp`:

```python
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

class JWTMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        try:
            jwt.decode(token, SECRET, algorithms=["HS256"])
        except Exception:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

app.api.add_middleware(JWTMiddleware)
```

---

## serve_file() — return files from tools

MCP tools can only return text. `serve_file()` bridges that — serialise any object
to a file, serve it via a download URL, MCPark previews it inline.

```python
@app.create_tool_api("/report")
def sales_report() -> str:
    """Generate a sales report. Use when the user asks for sales data."""
    import pandas as pd
    df = pd.DataFrame({"Month": ["Jan", "Feb"], "Sales": [1200, 1500]})
    return app.serve_file("report.xlsx", df, port=8000)

@app.create_tool_api("/chart")
def sales_chart() -> str:
    """Generate a sales chart."""
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots()
    ax.bar(["Jan", "Feb"], [1200, 1500])
    return app.serve_file("chart.png", fig, port=8000)
```

Supported content types:

| Pass as `content`       | Result                              |
|------------------------|-------------------------------------|
| `pandas.DataFrame`     | `.xlsx`, `.csv`, or `.json` by ext  |
| `matplotlib.Figure`    | `.png` or `.pdf` by ext             |
| `bytes`                | written directly                    |
| `"http://..."`         | downloaded from URL and re-served   |
| `"/path/to/file"`      | served directly from disk           |

Served files expire after `file_ttl` seconds (default 3600) and the store is capped at
`file_store_max` entries (default 256), evicting oldest first — so a long-running server
doesn't leak RAM or temp files. Temp copies simcpi created are deleted on eviction; paths
you passed in are never touched.

Install extras for DataFrame/Figure support:
```bash
pip install simcpi[files]
```

---

## Connecting to Claude Desktop and Cursor

```python
# Local dev — writes config file and opens the app
app.connect_claude(port=8000, launch=True)   # opens Claude Desktop + Ctrl+R notification
app.connect_cursor(port=8000, launch=True)   # opens Cursor + restart notification

# Remote/production — pass full URL
app.connect_claude(url="https://myapi.com/mcp/mcp")
app.connect_cursor(url="https://myapi.com/mcp/mcp")

# Standalone — no MCPApi instance needed
from simcpi import connect_claude, connect_cursor
connect_claude("my-api", 8000, True)          # positional: title, port, launch
connect_cursor("my-api", 8000, True)
```

`launch=True`:
- Finds and opens Claude Desktop / Cursor automatically
- Shows a native OS notification (macOS: notification centre, Windows: system tray)
- If app not found: prints install link, no notification shown

After calling `connect_claude()`, restart Claude Desktop to apply (or press Ctrl+R).

---

## MCPark — visual testing UI

Open `http://localhost:8000/mcpark` in any browser.

- **Sidebar** — MCP Server field (local or any remote URL + bearer, with a LOCAL/REMOTE
  pill), provider/key/model config, Claude Desktop snippet
- **Prompt bar** — type a natural language prompt, the LLM calls the right tool live
- **Assistant toggle** — off gives raw tool output, on gives LLM interpretation
- **File preview** — images show inline (including base64 MCP image content), Excel
  renders as table, PDFs open in-page
- **✨ Generate Test Cases** — auto-generates single-tool and multi-tool test prompts
- **👍 / 👎 + reason** (when `feedback=True`) — rate each run, optionally say why
- **📝 Improve Docstrings** (when `feedback=True`) — score & rewrite tool descriptions
  against your ratings; Copy / Apply (live) / Write to source
- **🗄 View Feedback DB** / **🗑 Clear DB** (when `feedback=True`) — browse or reset the
  feedback table
- Responsive: composer wraps on narrow windows; sidebar stacks below ~700px

---

## Clients

### QuickClient — sync, simple, auto-downloads files

```python
from simcpi import QuickClient

client = QuickClient(
    mcp_server="http://localhost:8000/mcp/mcp",
    provider="openai",
    api_key="sk-...",
    model="gpt-4o",
)

# plain text result
answer = client.run("Greet Mohan in Telugu")

# file result — auto-detected and downloaded
result = client.run("Give me the sales report")
result.save("report.xlsx")   # saves to disk

# list available tools
client.tools()   # {"http://...": ["greet", "report"]}

# multi-server
client = QuickClient(
    mcp_server=["http://localhost:8000/mcp/mcp", "http://localhost:8080/mcp/mcp"],
    provider="openai", api_key="sk-...",
)
```

### MCPClient — async, full trace

```python
import asyncio
from simcpi import MCPClient

client = MCPClient(
    mcp_server="http://localhost:8000/mcp/mcp",
    provider="anthropic",
    api_key="sk-ant-...",
)

result = asyncio.run(client.run("Add 42 and 58"))
print(result.answer)        # "The sum is 100."
print(result.tools_called)  # [ToolCall(name="add", arguments={...}, result="100")]
print(result.success)       # True
print(result.error)         # None
```

### get_client() — in-process (skip HTTP)

```python
client = app.get_client()   # inherits provider/api_key/model from MCPApi
result = await client.run("Add 42 and 58")
```

---

## Auth — bearer token (`auth_token`)

Opt-in, default off. One argument protects **every** route — REST, MCP, MCPark, `/files`.

```python
app = MCPApi(title="my-api", auth_token="my-secret-token")
```

Accepted credentials (checked in order):
- `Authorization: Bearer my-secret-token` — MCP clients, curl, scripts
- `?key=my-secret-token` — first browser visit; plants a `simcpi_key` cookie
- `simcpi_key` cookie — subsequent browser requests (so MCPark keeps working)

Clients and config helpers carry it automatically:
```python
QuickClient("http://localhost:8000/mcp/mcp", provider="openai", api_key="sk-...",
            auth_token="my-secret-token")
MCPClient(..., auth_token="my-secret-token")

app.connect_claude(port=8000)   # writes the bearer header into the Claude config
app.connect_cursor(port=8000)   # same for Cursor
```

For custom schemes (JWT, OAuth) add your own `BaseHTTPMiddleware` to `app.api` — it
runs on all paths including `/mcp/mcp`. `Depends()` stripping (above) still gives you
REST-only parameter injection alongside any middleware.

---

## Feedback memory — log → rate → retrieve

Opt-in (`feedback=True`). MCPark logs every run as `prompt → tool(s) + params → rating`
in local SQLite, then steers future runs with similar rated examples.

```python
app = MCPApi(title="my-api", provider="openai", api_key="sk-...", feedback=True)
```

- Each MCPark run shows a **👍 / 👎** block with an optional **"why?"** reason note.
- On the next run simcpi embeds the prompt, retrieves the top-`feedback_k` past prompts
  above `feedback_threshold` cosine similarity, and injects them as few-shot hints
  (labelling 👎 examples loudly as "do differently").
- A **🧠 Recalled from feedback** block shows what informed the run + similarity %.
- **🗄 View Feedback DB** browses the raw table; **🗑 Clear DB** wipes it (localhost-only).
- Retrieval needs an OpenAI-compatible embeddings key (`embedding_api_key`, or reuses
  `api_key` when provider is OpenAI). Without one it degrades to log-and-rate, no retrieval.
- Default DB path: `./simcpi_feedback.db`. Brute-force numpy-free cosine, one vector/row.

The reason notes are the strongest signal for the optimizer below.

---

## Docstring optimizer — "📝 Improve Docstrings"

In MCPark (visible when `feedback=True`), after you've rated some runs. For each tool it:
turns rated runs into should/should-not expectations (plus your written reasons),
replays them through a selection-only LLM call to score the current description,
rewrites it, re-scores, and keeps the rewrite only if `candidate% > current%`.

Three actions per improved result:
- **📋 Copy** — paste it in yourself
- **⚡ Apply (live)** — updates the running tool's description (until restart)
- **💾 Write to source** — rewrites the docstring straight into your `.py` file
  (AST-located, body untouched) and updates live. **Localhost-only** (403 from a
  remote/exposed MCPark — it never edits source over the network).

Tools without enough rated runs report "skipped — needs rated runs".

---

## Remote inspector — MCPark / `mcpark` for any MCP server

MCPark speaks the MCP protocol over HTTP, so it works against servers you didn't build.

```bash
mcpark https://mcp.deepwiki.com/mcp                 # console command (after pip install)
mcpark http://127.0.0.1:9000/mcp/mcp --auth tok --port 8123
python -m simcpi https://mcp.deepwiki.com/mcp       # equivalent
```

Flags: `--auth`, `--port` (default 8123), `--provider`, `--api-key`, `--model`, `--base-url`.
In the MCPark UI, the **MCP Server** field also takes any URL + optional bearer token and a
🔌 Connect button; a LOCAL/REMOTE pill shows where you're pointed. Remote targets get
explore + test + feedback + optimizer scoring + Copy; Apply-live and Write-to-source need
the target in-process (your own simcpi server).

---

## Common patterns

**Return structured data — use a Pydantic model as return type:**
```python
from pydantic import BaseModel

class Report(BaseModel):
    total: float
    rows: list[dict]

@app.create_tool_api("/summary")
def get_summary() -> Report:
    """Get sales summary."""
    return Report(total=4500.0, rows=[...])
```

**Tool that serves a file and returns its URL:**
```python
@app.create_tool_api("/export")
def export_data(format: str = "xlsx") -> str:
    """Export data. Use when the user asks to download or export data."""
    df = get_dataframe()
    return app.serve_file(f"export.{format}", df, port=8000)
```

**Expose existing FastAPI routes without MCP:**
```python
# Use app.api directly — won't register as MCP tool
@app.api.get("/health")
def health():
    return {"status": "ok"}
```

**List registered tools:**
```python
app.list_tools()   # ["greet", "add", "report"]
```

---

## Gotchas

**fastmcp version** — simcpi accepts `fastmcp>=3.2` (verified on 3.2.4 and 3.4.2).
If an upgrade leaves `from fastmcp import FastMCP` broken ("unknown location"),
run `pip install --force-reinstall "fastmcp-slim[client,server]"` — a pip
upgrade-ordering quirk, not a simcpi issue.

**QuickClient breaks in Jupyter** — `asyncio.run()` fails inside a running event loop.
Use `MCPClient` with `await` instead.

**Tool docstrings are LLM instructions** — vague docstrings mean the LLM won't know
when to call the tool. Always include a "Use this when..." sentence.

**`serve_file()` host/port must match your server** — if running on 0.0.0.0:8080
pass `host="0.0.0.0", port=8080`. Default is `localhost:8000`.

**Claude web requires a public URL** — `connect_claude()` + localhost only works
for Claude Desktop. For claude.ai, deploy your server publicly and pass the URL:
`app.connect_claude(url="https://your-server.com/mcp/mcp")`.

**Depends() params are None on MCP calls** — if your tool uses the injected value
for more than auth (e.g. `return f"Hello {user.name}"`), handle the None case.

**MCPark API key is never sent to the browser** — `api_key_configured: true` is
exposed but the actual key is not. Safe to set on the server.

**Write-to-source and Clear-DB are localhost-only** — both edit/destroy local state,
so MCPark refuses them (403) for any non-loopback caller. An exposed/ngrok'd MCPark can
test and rate but cannot rewrite your source or wipe your DB.

**Feedback retrieval is OpenAI-embeddings-only** — `feedback=True` logs and rates with
any provider, but the kNN retrieval needs an OpenAI-compatible embeddings endpoint. On an
Anthropic-only setup, pass a separate `embedding_api_key` or retrieval stays off.

**Don't switch `embedding_model` after collecting data** — it's fixed per DB (no per-run
override); all stored vectors must share one embedding space. Switching silently breaks
retrieval: a *different-dimension* model leaves old vectors safely ignored (backfill only
fills **missing** embeddings, never re-embeds), and a *same-dimension different* model mixes
spaces so cosine becomes noise — with nothing flagging it. If you must change models, **🗑
Clear DB** (or re-embed) first. You choose the model but not a custom output `dimensions`
(simcpi passes only `model` + `input`), so you get each model's native width.

**Running a script from a subfolder can import a stale installed simcpi** — `python sub/x.py`
puts `sub/` on `sys.path[0]`, so `import simcpi` may pick up a pip-installed copy instead of
your working tree. Use `pip install -e .` so the installed package *is* your source, or add
`sys.path.insert(0, repo_root)` at the top of throwaway scripts.
