"""
mcpserver.py — simcpi server
============================
Single @app.create_tool_api() decorator → REST route + MCP tool.

Routes added automatically:
  GET  /docs          Swagger UI
  GET  /mcpark        MCPark — visual explorer + LLM tester
  POST /mcpark/tools  List registered MCP tools (internal)
  POST /mcpark/run    Run LLM+MCP loop (internal)
  POST /mcp/mcp       MCP streamable-http transport
"""

from __future__ import annotations

import json
import inspect
import sys
from inspect import cleandoc
import functools
from contextlib import asynccontextmanager
from typing import Callable, Literal, get_type_hints

# ── Dependency check ──────────────────────────────────────────────────────────
_REQUIRED = {
    "fastapi": "fastapi>=0.100.0",
    "fastmcp": "fastmcp=3.2.4",
    "pydantic": "pydantic>=2.0.0",
    "uvicorn": "uvicorn>=0.20.0",
}
_missing = []
for _pkg, _spec in _REQUIRED.items():
    try:
        __import__(_pkg)
    except ImportError:
        _missing.append(_spec)
if _missing:
    print("[simcpi] Missing dependencies. Run: pip install " + " ".join(_missing))
    sys.exit(1)
# ─────────────────────────────────────────────────────────────────────────────

import uuid
import pathlib
import tempfile

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastmcp import FastMCP
from fastmcp import Client as _MCPInternalClient
from fastmcp.client import StreamableHttpTransport
from pydantic import BaseModel


# ─────────────────────────────────────────────────────────────────────────────
# MIME type helper
# ─────────────────────────────────────────────────────────────────────────────

def _mime_type(filename: str) -> str:
    ext = pathlib.Path(filename).suffix.lower()
    return {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls":  "application/vnd.ms-excel",
        ".csv":  "text/csv",
        ".pdf":  "application/pdf",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif":  "image/gif",
        ".webp": "image/webp",
        ".json": "application/json",
        ".txt":  "text/plain",
        ".zip":  "application/zip",
    }.get(ext, "application/octet-stream")


# ─────────────────────────────────────────────────────────────────────────────
# MCPark HTML — served at /mcpark
# ─────────────────────────────────────────────────────────────────────────────

def _load_mcpark_html() -> str:
    """Load mcpark.html from static/ folder alongside mcpserver.py."""
    import pathlib as _p, os as _o
    here = _p.Path(_o.path.abspath(__file__)).parent
    f = here / "static" / "mcpark.html"
    if f.exists():
        return f.read_text(encoding="utf-8")
    raise FileNotFoundError(f"mcpark.html not found at {f}")


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    prompt:          str
    api_key:         str | None = None
    provider:        Literal["openai", "anthropic"] | None = None
    model:           str = "gpt-4o"
    base_url:        str | None = None
    default_headers: dict | None = None
    assistant:       bool = True   # True: LLM explains result. False: raw tool output only.


# ─────────────────────────────────────────────────────────────────────────────
# MCPApi — the server class
# ─────────────────────────────────────────────────────────────────────────────

class MCPApi:
    """
    Merges FastAPI and FastMCP into a single server.

    Parameters
    ----------
    title:          API / MCP server name.
    mcp_path:       MCP transport prefix (default: "/mcp").
    version:        Version string.
    description:    Shown in OpenAPI docs and MCP server instructions.
    mcpark_path:    Path for MCPark UI (default: "/mcpark"). None to disable.
    provider:       Default LLM provider — "openai" or "anthropic".
    api_key:        Default LLM API key. Inherited by MCPark UI + get_client().
    base_url:       Default LLM base URL (for proxies like AICredits, OpenRouter).
    model:          Default model name. Shown pre-selected in MCPark.
    """

    def __init__(
        self,
        title:        str = "simcpi",
        mcp_path:     str = "/mcp",
        version:      str = "0.1.0",
        description:  str | None = None,
        mcpark_path:  str | None = "/mcpark",
        provider:         Literal["openai", "anthropic"] | None = None,
        api_key:          str | None = None,
        base_url:         str | None = None,
        model:            str | None = None,
        default_headers:  dict | None = None,
    ):
        self.title           = title
        self.mcp_path        = mcp_path.rstrip("/")
        self.mcpark_path     = mcpark_path
        self.provider        = provider
        self.api_key         = api_key
        self.base_url        = base_url
        self.model           = model
        self.default_headers = default_headers

        # ── FastMCP ──────────────────────────────────────────────────────────
        self.mcp     = FastMCP(name=title, version=version, instructions=description)
        self._mcp_app = self.mcp.http_app()

        # ── FastAPI + wired MCP lifespan ─────────────────────────────────────
        mcp_app = self._mcp_app

        @asynccontextmanager
        async def combined_lifespan(app: FastAPI):
            async with mcp_app.lifespan(mcp_app):
                yield

        self.api = FastAPI(
            title=title,
            version=version,
            description=description or "",
            lifespan=combined_lifespan,
        )

        self.api.mount(self.mcp_path, self._mcp_app)

        # ── Routes ───────────────────────────────────────────────────────────
        self._register_mcpark()

        # ── Registry ─────────────────────────────────────────────────────────
        self._registry: dict[str, Callable] = {}

        # ── File store — maps token → filepath ───────────────────────────────
        self._file_store: dict[str, pathlib.Path] = {}
        self._register_files_route()

    # ── MCPark routes ─────────────────────────────────────────────────────────

    def _register_mcpark(self):
        if not self.mcpark_path:
            return

        path = self.mcpark_path

        # Build server config dict — injected into the HTML so UI pre-fills
        def _server_config(self=self) -> dict:
            return {
                "provider":          self.provider or "openai",
                "api_key_configured": bool(self.api_key),  # never expose the actual key in HTML
                "base_url":          self.base_url,
                "model":             self.model or "gpt-4o",
                "mcp_path":          self.mcp_path,
                "title":             self.title,
            }

        @self.api.get(path, response_class=HTMLResponse, include_in_schema=False)
        async def mcpark_ui():
            import json as _json
            config_json = _json.dumps(_server_config())
            html = _load_mcpark_html().replace("__SERVER_CONFIG__", config_json)
            return HTMLResponse(html)

        @self.api.post(f"{path}/tools", include_in_schema=False)
        async def mcpark_tools():
            async with _MCPInternalClient(self.mcp) as client:
                tools = await client.list_tools()
            return JSONResponse({
                "tools": [
                    {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                    for t in tools
                ]
            })

        @self.api.post(f"{path}/run", include_in_schema=False)
        async def mcpark_run(req: RunRequest, request: Request):
            # Resolve — request body wins, instance defaults as fallback
            resolved_provider = req.provider or self.provider
            resolved_api_key  = req.api_key  or self.api_key
            resolved_base_url = req.base_url or self.base_url
            resolved_model    = req.model    or self.model or "gpt-4o"

            resolved_headers  = req.default_headers or self.default_headers

            if not resolved_provider or not resolved_api_key:
                return JSONResponse({
                    "error": "provider and api_key are required — set on MCPApi() or pass in the UI",
                    "trace": [], "tools_called": False
                })

            base    = str(request.base_url).rstrip("/")
            mcp_url = f"{base}{self.mcp_path}/mcp"

            # Fetch tools via real HTTP transport
            transport = StreamableHttpTransport(mcp_url)
            async with _MCPInternalClient(transport) as client:
                raw_tools = await client.list_tools()

            mcp_tools = [
                {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                for t in raw_tools
            ]

            trace:        list[dict] = []
            tools_called: bool      = False

            try:
                if resolved_provider == "openai":
                    from openai import AsyncOpenAI
                    kw = dict(api_key=resolved_api_key)
                    if resolved_base_url:
                        kw["base_url"] = resolved_base_url
                    if resolved_headers:
                        kw["default_headers"] = resolved_headers
                    oai = AsyncOpenAI(**kw)

                    oai_tools = [
                        {"type": "function", "function": {
                            "name":        t["name"],
                            "description": t.get("description", ""),
                            "parameters":  t.get("inputSchema", {"type": "object", "properties": {}}),
                        }}
                        for t in mcp_tools
                    ]
                    messages = [{"role": "user", "content": req.prompt}]

                    while True:
                        resp = await oai.chat.completions.create(
                            model=resolved_model, messages=messages,
                            tools=oai_tools, tool_choice="auto",
                        )
                        msg = resp.choices[0].message
                        messages.append(msg)

                        if msg.tool_calls:
                            tools_called = True
                            results = []
                            for tc in msg.tool_calls:
                                import json as _j
                                args = _j.loads(tc.function.arguments)
                                trace.append({"type": "tool_call", "name": tc.function.name, "arguments": args})
                                async with _MCPInternalClient(StreamableHttpTransport(mcp_url)) as c:
                                    r = await c.call_tool(tc.function.name, args)
                                txt = r.content[0].text if r.content else str(r)
                                trace.append({"type": "tool_result", "name": tc.function.name, "result": txt})
                                results.append({"tool_call_id": tc.id, "role": "tool", "content": txt})
                            messages.extend(results)
                            if not req.assistant:
                                break   # raw tool output only — skip LLM interpretation
                        else:
                            if req.assistant and msg.content:
                                trace.append({"type": "message", "content": msg.content})
                            break

                else:  # anthropic
                    import anthropic as _sdk
                    kw = dict(api_key=resolved_api_key)
                    if resolved_base_url:
                        kw["base_url"] = resolved_base_url
                    if resolved_headers:
                        kw["default_headers"] = resolved_headers
                    ant = _sdk.AsyncAnthropic(**kw)

                    ant_tools = [
                        {"name": t["name"], "description": t.get("description", ""),
                         "input_schema": t.get("inputSchema", {"type": "object", "properties": {}})}
                        for t in mcp_tools
                    ]
                    messages = [{"role": "user", "content": req.prompt}]

                    while True:
                        resp = await ant.messages.create(
                            model=resolved_model, max_tokens=1024,
                            tools=ant_tools, messages=messages,
                        )
                        asst_content, tool_blocks = [], []
                        for block in resp.content:
                            if block.type == "text":
                                asst_content.append({"type": "text", "text": block.text})
                                if req.assistant and block.text:
                                    trace.append({"type": "message", "content": block.text})
                            elif block.type == "tool_use":
                                tools_called = True
                                asst_content.append({"type": "tool_use", "id": block.id,
                                                     "name": block.name, "input": block.input})
                                trace.append({"type": "tool_call", "name": block.name, "arguments": block.input})
                                tool_blocks.append(block)

                        messages.append({"role": "assistant", "content": asst_content})

                        if tool_blocks:
                            results = []
                            for block in tool_blocks:
                                async with _MCPInternalClient(StreamableHttpTransport(mcp_url)) as c:
                                    r = await c.call_tool(block.name, block.input)
                                txt = r.content[0].text if r.content else str(r)
                                trace.append({"type": "tool_result", "name": block.name, "result": txt})
                                results.append({"type": "tool_result", "tool_use_id": block.id, "content": txt})
                            messages.append({"role": "user", "content": results})
                            if not req.assistant:
                                break   # raw tool output only — skip LLM interpretation
                        else:
                            break

            except Exception as e:
                return JSONResponse({"error": str(e), "trace": trace, "tools_called": tools_called})

            return JSONResponse({"trace": trace, "tools_called": tools_called})

    # ── File serving ─────────────────────────────────────────────────────────

    def _register_files_route(self):
        """Register GET /files/{token}/{filename} for served files."""

        @self.api.get("/files/{token}/{filename}", include_in_schema=False)
        async def serve_file_route(token: str, filename: str):
            path = self._file_store.get(token)
            if not path or not path.exists():
                return JSONResponse({"error": "File not found or expired"}, status_code=404)
            return FileResponse(
                path=str(path),
                filename=filename,
                media_type=_mime_type(filename),
            )

    def serve_file(
        self,
        filename: str,
        content,
        host:     str = "localhost",
        port:     int = 8000,
    ) -> str:
        """
        Serve a file through FastAPI and return its URL.
        Pass your object directly — simcpi handles serialization automatically.
        MCPark will preview images/PDFs inline and show a download button for others.

        Parameters
        ----------
        filename: Name of the file e.g. "report.xlsx", "chart.png"
        content:  Your object — pandas DataFrame, matplotlib Figure,
                  raw bytes, or a file path string.
        host:     Host where this server runs (default: localhost).
        port:     Port where this server runs (default: 8000).

        Returns
        -------
        str — URL the client/MCPark can open or download.

        Usage:
            # pandas DataFrame → Excel
            return app.serve_file("report.xlsx", df)

            # pandas DataFrame → CSV
            return app.serve_file("report.csv", df)

            # matplotlib Figure → PNG/PDF
            return app.serve_file("chart.png", fig)

            # existing file on disk
            return app.serve_file("report.pdf", "/path/to/report.pdf")

            # raw bytes
            return app.serve_file("data.zip", zip_bytes)
        """
        import io as _io

        ext = pathlib.Path(filename).suffix.lower()

        # ── Serialise based on object type + extension ────────────────────────
        if isinstance(content, (str, pathlib.Path)):
            path_str = str(content)
            if path_str.startswith("http://") or path_str.startswith("https://"):
                # Remote URL — download and serve
                import urllib.request as _req
                with _req.urlopen(path_str) as r:
                    data = r.read()
                # fall through to raw bytes path below
                token     = uuid.uuid4().hex
                tmp_dir   = pathlib.Path(tempfile.gettempdir()) / "simcpi_files"
                tmp_dir.mkdir(exist_ok=True)
                file_path = tmp_dir / f"{token}_{filename}"
                file_path.write_bytes(data)
                self._file_store[token] = file_path
                return f"http://{host}:{port}/files/{token}/{filename}"
            else:
                # Local file path — serve directly, no copy needed
                file_path = pathlib.Path(content)
                token     = uuid.uuid4().hex
                self._file_store[token] = file_path
                return f"http://{host}:{port}/files/{token}/{filename}"

        elif isinstance(content, bytes):
            data = content

        else:
            # Try pandas DataFrame
            try:
                import pandas as _pd
                if isinstance(content, _pd.DataFrame):
                    buf = _io.BytesIO()
                    if ext in (".xlsx", ".xls"):
                        content.to_excel(buf, index=False)
                    elif ext == ".csv":
                        buf.write(content.to_csv(index=False).encode())
                    elif ext == ".json":
                        buf.write(content.to_json(orient="records").encode())
                    else:
                        content.to_excel(buf, index=False)
                    data = buf.getvalue()
                    data  # assigned below
                    token = uuid.uuid4().hex
                    tmp_dir   = pathlib.Path(tempfile.gettempdir()) / "simcpi_files"
                    tmp_dir.mkdir(exist_ok=True)
                    file_path = tmp_dir / f"{token}_{filename}"
                    file_path.write_bytes(data)
                    self._file_store[token] = file_path
                    return f"http://{host}:{port}/files/{token}/{filename}"
            except ImportError:
                pass

            # Try matplotlib Figure
            try:
                import matplotlib.pyplot as _plt
                from matplotlib.figure import Figure as _Figure
                if isinstance(content, _Figure):
                    buf = _io.BytesIO()
                    fmt = ext.lstrip(".") or "png"
                    content.savefig(buf, format=fmt, bbox_inches="tight")
                    data = buf.getvalue()
                    token = uuid.uuid4().hex
                    tmp_dir   = pathlib.Path(tempfile.gettempdir()) / "simcpi_files"
                    tmp_dir.mkdir(exist_ok=True)
                    file_path = tmp_dir / f"{token}_{filename}"
                    file_path.write_bytes(data)
                    self._file_store[token] = file_path
                    return f"http://{host}:{port}/files/{token}/{filename}"
            except ImportError:
                pass

            raise TypeError(
                f"[simcpi] serve_file() doesn't know how to serialize {type(content).__name__}. "
                "Pass a DataFrame, matplotlib Figure, bytes, or a file path."
            )

        # ── Write raw bytes ───────────────────────────────────────────────────
        token     = uuid.uuid4().hex
        tmp_dir   = pathlib.Path(tempfile.gettempdir()) / "simcpi_files"
        tmp_dir.mkdir(exist_ok=True)
        file_path = tmp_dir / f"{token}_{filename}"
        file_path.write_bytes(data)
        self._file_store[token] = file_path
        return f"http://{host}:{port}/files/{token}/{filename}"

    # ── @create_tool_api decorator ────────────────────────────────────────────

    def create_tool_api(
        self,
        path: str,
        *,
        method:    Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST",
        tool_name: str | None = None,
        tags:      list[str] | None = None,
        summary:   str | None = None,
    ) -> Callable:
        """
        Register a function as both a FastAPI REST endpoint and a FastMCP tool.

        The function's docstring becomes the MCP tool description —
        write it for the LLM, not just for humans.

        Usage:
            @app.create_tool_api("/add", method="POST")
            def add(a: int, b: int) -> int:
                \"\"\"
                Add two numbers.
                Use this when the user asks to sum or total numbers.
                \"\"\"
                return a + b
        """
        def decorator(fn: Callable) -> Callable:
            name = tool_name or fn.__name__
            doc  = cleandoc(fn.__doc__) if fn.__doc__ else ""

            # 1. FastMCP tool
            self.mcp.tool(name=name, description=doc)(fn)

            # 2. FastAPI route
            @functools.wraps(fn)
            async def route_handler(*args, **kwargs):
                if inspect.iscoroutinefunction(fn):
                    return await fn(*args, **kwargs)
                return fn(*args, **kwargs)

            route_handler.__annotations__ = get_type_hints(fn)
            route_handler.__doc__ = doc

            getattr(self.api, method.lower())(
                path, summary=summary or name,
                description=doc, tags=tags, name=name,
            )(route_handler)

            self._registry[name] = fn
            return fn

        return decorator

    # ── ASGI passthrough ──────────────────────────────────────────────────────

    async def __call__(self, scope, receive, send):
        await self.api(scope, receive, send)

    # ── get_client — returns a wired MCPClient ────────────────────────────────

    def get_client(
        self,
        provider: Literal["openai", "anthropic"] | None = None,
        api_key:  str | None = None,
        model:    str | None = None,
        system:   str | None = None,
        timeout:  int = 30,
        base_url: str | None = None,
    ) -> "MCPClient":
        """
        Return an MCPClient pre-wired to this server's MCP instance (in-process).

        Falls back to provider/api_key/base_url/model set on MCPApi().

        Usage:
            app = MCPApi(provider="openai", api_key="sk-...")
            client = app.get_client()
            result = await client.run("Add 42 and 58")
        """
        from .mcpclient import MCPClient

        resolved_provider = provider or self.provider
        resolved_api_key  = api_key  or self.api_key
        resolved_base_url = base_url or self.base_url
        resolved_model    = model    or self.model

        if not resolved_provider:
            raise ValueError("provider required — set on MCPApi() or pass to get_client()")
        if not resolved_api_key:
            raise ValueError("api_key required — set on MCPApi() or pass to get_client()")

        return MCPClient(
            mcp_server=self.mcp,
            provider=resolved_provider,
            api_key=resolved_api_key,
            model=resolved_model,
            system=system,
            timeout=timeout,
            base_url=resolved_base_url,
            default_headers=self.default_headers,
        )

    def list_tools(self) -> list[str]:
        """Return names of all registered tools."""
        return list(self._registry.keys())

    def connect_claude(
        self,
        host:    str  = "localhost",
        port:    int  = 8000,
        force:   bool = False,
        native:  bool = False,
        url:     str | None = None,
    ) -> str:
        """
        Register this MCP server in Claude Desktop config automatically.
        Restart Claude Desktop after calling this.

        Parameters
        ----------
        url:     Full MCP URL — use this for remote servers e.g. "https://myapi.com/mcp/mcp".
                 If provided, host and port are ignored.
        host:    Host where this server runs (default: localhost).
        port:    Port where this server runs (default: 8000).
        force:   Overwrite existing entry with same name (default: False).
        native:  True  — write {"type":"streamableHttp","url":...} (newer Claude Desktop builds).
                 False — write {"command":"npx","args":["mcp-remote",...]} (default, works everywhere).
        """
        import json, pathlib, platform, os

        system = platform.system()
        if system == "Darwin":
            config_path = pathlib.Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
        elif system == "Windows":
            config_path = pathlib.Path(os.environ["APPDATA"]) / "Claude/claude_desktop_config.json"
        else:
            config_path = pathlib.Path.home() / ".config/Claude/claude_desktop_config.json"

        config = {}
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                config = {}
        else:
            config_path.parent.mkdir(parents=True, exist_ok=True)

        if "mcpServers" not in config:
            config["mcpServers"] = {}

        mcp_url   = url if url else f"http://{host}:{port}{self.mcp_path}/mcp"
        entry_key = f"{self.title}:{port}" if not url else self.title

        if entry_key in config["mcpServers"] and not force:
            existing_url = (config["mcpServers"][entry_key].get("args") or [None, None])[1] or ""
            if existing_url == mcp_url:
                print(f"[simcpi] '{entry_key}' already up to date in Claude config.")
                return str(config_path)

        if native:
            entry = {"type": "streamableHttp", "url": mcp_url}
        else:
            entry = {"command": "npx", "args": ["mcp-remote", mcp_url]}

        config["mcpServers"][entry_key] = entry
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        fmt = "native streamableHttp" if native else "mcp-remote (npx)"
        print(f"[simcpi] Connected '{entry_key}' to Claude Desktop  [{fmt}]")
        print(f"[simcpi] MCP URL : {mcp_url}")
        print(f"[simcpi] Config  : {config_path}")
        print(f"[simcpi] Restart Claude Desktop to apply.")
        return str(config_path)


    def export_mcpb(
        self,
        output_path: str | None = None,
        host: str = "localhost",
        port: int = 8000,
        version: str = "0.1.0",
        description: str | None = None,
    ) -> str:
        """
        Export this MCP server as a .mcpb bundle for one-click install in Claude Desktop.

        The generated .mcpb file can be:
          - Double-clicked to install in Claude Desktop
          - Dragged into the Claude Desktop window
          - Shared with your team

        Parameters
        ----------
        output_path:  Where to save the .mcpb file. Defaults to ./<title>.mcpb
        host:         Host where this HTTP server runs (for the manifest URL).
        port:         Port where this HTTP server runs.
        version:      Bundle version string.
        description:  Bundle description shown during install.

        Returns
        -------
        Path to the generated .mcpb file.

        Usage:
            app = MCPApi(title="My API")

            @app.create_tool_api("/add", method="POST")
            def add(a: int, b: int) -> int:
                \"\"\"Add two numbers.\"\"\"
                return a + b

            app.export_mcpb()  # generates My API.mcpb
        """
        import json, zipfile, textwrap, pathlib as _pl

        title       = self.title
        safe_title  = title.replace(" ", "_")
        out         = _pl.Path(output_path or f"{safe_title}.mcpb")
        mcp_url     = f"http://{host}:{port}{self.mcp_path}/mcp"
        desc        = description or f"{title} — generated by simcpi"

        # ── Build tool list for manifest ──────────────────────────────────────
        tools_manifest = []
        for name, fn in self._registry.items():
            from inspect import cleandoc, signature
            import typing
            doc = cleandoc(fn.__doc__) if fn.__doc__ else ""
            sig = signature(fn)
            props = {}
            required = []
            hints = typing.get_type_hints(fn)
            for pname, param in sig.parameters.items():
                ptype = hints.get(pname, str)
                type_map = {int: "integer", float: "number", str: "string", bool: "boolean"}
                props[pname] = {"type": type_map.get(ptype, "string")}
                if param.default is param.empty:
                    required.append(pname)
            tools_manifest.append({
                "name":        name,
                "description": doc,
                "inputSchema": {
                    "type":       "object",
                    "properties": props,
                    "required":   required,
                }
            })

        # ── manifest.json ─────────────────────────────────────────────────────
        manifest = {
            "schema_version": "v1",
            "name":           safe_title,
            "display_name":   title,
            "version":        version,
            "description":    desc,
            "compatibility":  {"platforms": ["darwin", "win32"]},
            "server": {
                "type":    "http",
                "url":     mcp_url,
                "transport": "streamable-http",
            },
            "tools": tools_manifest,
        }

        # ── README inside the bundle ──────────────────────────────────────────
        readme = textwrap.dedent(f"""
            # {title}

            Generated by simcpi.

            ## Install
            Double-click {safe_title}.mcpb in Claude Desktop.

            ## MCP Server URL
            {mcp_url}

            ## Tools
            {chr(10).join(f"- {t['name']}: {t['description'].splitlines()[0] if t['description'] else ''}" for t in tools_manifest)}
        """).strip()

        # ── Write .mcpb (ZIP) ─────────────────────────────────────────────────
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("README.md",     readme)

        print(f"[simcpi] Exported {out}")
        print(f"[simcpi] Double-click {out} to install in Claude Desktop")
        print(f"[simcpi] Make sure your server is running at {mcp_url}")
        return str(out)
    def connect_cursor(
        self,
        host:    str  = "localhost",
        port:    int  = 8000,
        force:   bool = False,
        scope:   str  = "global",
        url:     str | None = None,
    ) -> str:
        """
        Register this MCP server in Cursor's MCP config automatically.
        Restart Cursor after calling this.

        Parameters
        ----------
        url:    Full MCP URL — use this for remote servers e.g. "https://myapi.com/mcp/mcp".
                If provided, host and port are ignored.
        host:   Host where this server runs (default: localhost).
        port:   Port where this server runs (default: 8000).
        force:  Overwrite existing entry with same key (default: False).
        scope:  "global"  — writes to ~/.cursor/mcp.json (default).
                "project" — writes to .cursor/mcp.json in current directory.
        """
        import json, pathlib, os

        if scope == "project":
            config_path = pathlib.Path.cwd() / ".cursor" / "mcp.json"
        else:
            config_path = pathlib.Path.home() / ".cursor" / "mcp.json"

        config = {}
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                config = {}
        else:
            config_path.parent.mkdir(parents=True, exist_ok=True)

        if "mcpServers" not in config:
            config["mcpServers"] = {}

        mcp_url   = url if url else f"http://{host}:{port}{self.mcp_path}/mcp"
        entry_key = f"{self.title}:{port}" if not url else self.title

        if entry_key in config["mcpServers"] and not force:
            existing_url = (config["mcpServers"][entry_key].get("args") or [None, None])[1] or ""
            if existing_url == mcp_url:
                print(f"[simcpi] '{entry_key}' already up to date in Cursor config.")
                return str(config_path)

        config["mcpServers"][entry_key] = {
            "command": "npx",
            "args":    ["mcp-remote", mcp_url],
        }
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        print(f"[simcpi] Connected '{entry_key}' to Cursor  [{scope}]")
        print(f"[simcpi] MCP URL : {mcp_url}")
        print(f"[simcpi] Config  : {config_path}")
        print(f"[simcpi] Restart Cursor to apply.")
        return str(config_path)

    def get_claude_config(self, host: str = "localhost", port: int = 8000, native: bool = False) -> dict:  # noqa: E501
        """
        Return the Claude Desktop config snippet for this server.

        Parameters
        ----------
        native: True — {"type":"streamableHttp","url":...} format.
                False — {"command":"npx","args":["mcp-remote",...]} format (default).
        """
        mcp_url = f"http://{host}:{port}{self.mcp_path}/mcp"
        if native:
            entry = {"type": "streamableHttp", "url": mcp_url}
        else:
            entry = {"command": "npx", "args": ["mcp-remote", mcp_url]}
        return {"mcpServers": {self.title: entry}}


# ─────────────────────────────────────────────────────────────────────────────
# Standalone connect helpers — no MCPApi instance needed
# ─────────────────────────────────────────────────────────────────────────────

def connect_claude(
    title:    str,
    port:     int  = 8000,
    url:      str | None = None,
    force:    bool = False,
    native:   bool = False,
    mcp_path: str  = "/mcp",
) -> str:
    """
    Register an MCP server in Claude Desktop config without an MCPApi instance.

    Parameters
    ----------
    title:    Server name — used as key in config e.g. "my-api".
    port:     Local port (ignored if url is given).
    url:      Full remote URL e.g. "https://myapi.com/mcp/mcp".
    force:    Overwrite existing entry (default: False).
    native:   True — streamableHttp format. False — mcp-remote (default).
    mcp_path: MCP path prefix (default: "/mcp").
    """
    import json, pathlib, platform, os

    system = platform.system()
    if system == "Darwin":
        config_path = pathlib.Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    elif system == "Windows":
        config_path = pathlib.Path(os.environ["APPDATA"]) / "Claude/claude_desktop_config.json"
    else:
        config_path = pathlib.Path.home() / ".config/Claude/claude_desktop_config.json"

    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            config = {}
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    mcp_url   = url if url else f"http://localhost:{port}{mcp_path}/mcp"
    entry_key = title if url else f"{title}:{port}"

    if entry_key in config["mcpServers"] and not force:
        existing_url = (config["mcpServers"][entry_key].get("args") or [None, None])[1] or ""
        if existing_url == mcp_url:
            print(f"[simcpi] '{entry_key}' already up to date in Claude config.")
            return str(config_path)

    entry = {"type": "streamableHttp", "url": mcp_url} if native else {"command": "npx", "args": ["mcp-remote", mcp_url]}
    config["mcpServers"][entry_key] = entry
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    print(f"[simcpi] Connected '{entry_key}' → {mcp_url}")
    print(f"[simcpi] Restart Claude Desktop to apply.")
    return str(config_path)


def connect_cursor(
    title:    str,
    port:     int  = 8000,
    url:      str | None = None,
    force:    bool = False,
    scope:    str  = "global",
    mcp_path: str  = "/mcp",
) -> str:
    """
    Register an MCP server in Cursor config without an MCPApi instance.

    Parameters
    ----------
    title:    Server name — used as key in config.
    port:     Local port (ignored if url is given).
    url:      Full remote URL e.g. "https://myapi.com/mcp/mcp".
    force:    Overwrite existing entry (default: False).
    scope:    "global" — ~/.cursor/mcp.json. "project" — .cursor/mcp.json.
    mcp_path: MCP path prefix (default: "/mcp").
    """
    import json, pathlib

    config_path = (pathlib.Path.home() / ".cursor" / "mcp.json") if scope == "global" \
                  else (pathlib.Path.cwd() / ".cursor" / "mcp.json")

    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            config = {}
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    mcp_url   = url if url else f"http://localhost:{port}{mcp_path}/mcp"
    entry_key = title if url else f"{title}:{port}"

    if entry_key in config["mcpServers"] and not force:
        existing_url = (config["mcpServers"][entry_key].get("args") or [None, None])[1] or ""
        if existing_url == mcp_url:
            print(f"[simcpi] '{entry_key}' already up to date in Cursor config.")
            return str(config_path)

    config["mcpServers"][entry_key] = {"command": "npx", "args": ["mcp-remote", mcp_url]}
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    print(f"[simcpi] Connected '{entry_key}' → {mcp_url}")
    print(f"[simcpi] Restart Cursor to apply.")
    return str(config_path)