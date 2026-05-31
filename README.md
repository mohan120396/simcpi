# simcpi

Building MCP tools today means maintaining two separate things — an API for your app and an MCP server for your AI client. simcpi collapses that into a single decorator.

Define a function once. Get a REST endpoint, an MCP tool, and a visual tester — all at the same time.

**Features:**
- `@create_tool_api` — one decorator registers both a FastAPI route and an MCP tool
- **MCPark** — built-in visual UI to inspect and test MCP tools in the browser
- `serve_file()` — return DataFrames, charts, images, or any file from an MCP tool in one line
- `QuickClient` — connect to any MCP server and run tools through an LLM in 5 lines
- `connect_claude()` / `connect_cursor()` — auto-register your server in Claude Desktop or Cursor config
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

---

### 2. MCPark — visual MCP testing UI

Open `http://localhost:8000/mcpark` in any browser.

- Lists all registered tools in the sidebar
- Run tools with an LLM in the loop — select provider, paste API key
- **Assistant toggle** — off gives raw tool output, on gives LLM interpretation
- File results render inline: images show as images, Excel as a table, PDFs open in-page
- One-click copy of the Claude Desktop config snippet

No separate install. No external service. Ships inside the package.

---

### 3. `serve_file()` — return files from MCP tools

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

Install extras for DataFrame and Figure support:
```bash
pip install simcpi[files]
```

---

### 4. `QuickClient` — sync MCP client in 5 lines

```python
from simcpi import QuickClient

client = QuickClient(
    mcp_server="http://localhost:8000/mcp/mcp",
    provider="openai",
    api_key="sk-...",
    model="gpt-4o",
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

### 5. `MCPClient` — async client with full trace

```python
import asyncio
from simcpi import MCPClient

client = MCPClient(
    mcp_server="http://localhost:8000/mcp/mcp",
    provider="anthropic",
    api_key="sk-ant-...",
    model="claude-sonnet-4-20250514",
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

### 6. `connect_claude()` — Claude Desktop integration

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
connect_claude("prod",     url="https://myapi.com/mcp/mcp")
```

Multiple servers append as separate entries — no collision. Restart Claude Desktop after calling.

---

### 7. `connect_cursor()` — Cursor IDE integration

```python
app.connect_cursor(port=8000)                        # global (~/.cursor/mcp.json)
app.connect_cursor(port=8000, scope="project")       # project (.cursor/mcp.json)

# standalone
from simcpi import connect_cursor
connect_cursor("my-api", port=8000)
connect_cursor("prod",   url="https://myapi.com/mcp/mcp")
```

---

### 8. `get_client()` — in-process client

When you're already inside the server process, skip the HTTP round-trip:

```python
client = app.get_client()
result = await client.run("Add 42 and 58")
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

---

## Requirements

- Python 3.10+
- fastapi, fastmcp, pydantic, uvicorn, openai, anthropic

> **Note:** simcpi currently requires `fastmcp==3.2.4` specifically. Newer versions introduce breaking changes that are being addressed — compatibility will be expanded in future releases.

Optional (`pip install simcpi[files]`):
- pandas + openpyxl — Excel / CSV output from DataFrames
- matplotlib — PNG / PDF output from Figures

---

## License

MIT
