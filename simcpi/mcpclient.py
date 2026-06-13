"""
mcpclient.py — simcpi client
=============================
Two clients for different use cases:

MCPClient  — async, returns MCPResult. Used internally by MCPApi.get_client().
QuickClient — sync, returns str or FileResult. The simple public-facing client.

Usage (QuickClient — recommended):
    from simcpi import QuickClient

    client = QuickClient(
        mcp_server="http://localhost:8000/mcp/mcp",
        provider="openai",
        api_key="sk-...",
    )
    answer = client.run("Greet Mohan in Telugu")
    print(answer)

    # Multiple servers
    client = QuickClient(
        mcp_server=[
            "http://localhost:8000/mcp/mcp",
            "http://localhost:8080/mcp/mcp",
        ],
        provider="openai",
        api_key="sk-...",
    )

    # File result
    result = client.run("Get me the sales report")
    result.save("report.xlsx")

Usage (MCPClient — advanced/async):
    import asyncio
    from simcpi import MCPClient

    client = MCPClient(
        mcp_server="http://localhost:8000/mcp/mcp",
        provider="openai",
        api_key="sk-...",
    )
    result = asyncio.run(client.run("Add 42 and 58"))
    print(result.answer)
    print(result.tools_called)
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import urllib.request
import pathlib
from dataclasses import dataclass
from typing import Literal

# ── Dependency check ──────────────────────────────────────────────────────────
_REQUIRED = {"fastmcp": "fastmcp>=3.0.0"}
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

from fastmcp import Client as _MCPInternalClient
from fastmcp.client import StreamableHttpTransport


# ─────────────────────────────────────────────────────────────────────────────
# MCPClient result types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    """A single tool invocation made by the LLM."""
    name:      str
    arguments: dict
    result:    str


@dataclass
class MCPResult:
    """
    Returned by MCPClient.run().

    Attributes
    ----------
    answer:       Final text response from the LLM.
    tools_called: Ordered list of every tool the LLM invoked.
    success:      False if an exception occurred.
    error:        Error message if success=False.
    """
    answer:       str
    tools_called: list[ToolCall]
    success:      bool = True
    error:        str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# FileResult — returned by QuickClient when a tool returns a file URL
# ─────────────────────────────────────────────────────────────────────────────

class FileResult:
    """
    Returned by QuickClient.run() when a tool returns a file URL.

    Attributes
    ----------
    url:      The original file URL.
    filename: The file name e.g. "report.xlsx"
    bytes:    Raw file bytes (downloaded automatically).

    Methods
    -------
    save(path):  Save the file to disk. Defaults to ./<filename>.
    """

    def __init__(self, url: str, filename: str, data: bytes):
        self.url      = url
        self.filename = filename
        self.bytes    = data

    def save(self, path: str | None = None) -> str:
        """
        Save the file to disk.

        Parameters
        ----------
        path: File path or directory. Defaults to ./<filename>.

        Returns
        -------
        str — absolute path where the file was saved.
        """
        dest = pathlib.Path(path) if path else pathlib.Path.cwd() / self.filename
        if dest.is_dir():
            dest = dest / self.filename
        dest.write_bytes(self.bytes)
        return str(dest.resolve())

    def __repr__(self):
        return f"FileResult(filename={self.filename!r}, size={len(self.bytes)} bytes, url={self.url!r})"


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

_FILE_URL_RE = re.compile(r"https?://[^\s]+/files/[a-f0-9]+/([^\s\"']+)", re.IGNORECASE)


def _extract_file_url(text: str) -> tuple[str, str] | None:
    m = _FILE_URL_RE.search(text)
    return (m.group(0), m.group(1)) if m else None


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


# ─────────────────────────────────────────────────────────────────────────────
# MCPClient — async, full result, used by MCPApi.get_client()
# ─────────────────────────────────────────────────────────────────────────────

class MCPClient:
    """
    Async MCP client — connects to any MCP server and wires it to an LLM.
    Returns MCPResult with full tool call trace.

    For a simpler sync interface, use QuickClient instead.

    Parameters
    ----------
    mcp_server:  MCP server URL or FastMCP instance (in-process).
    provider:    "openai" or "anthropic"
    api_key:     Your LLM API key.
    model:       Model name. Defaults: gpt-4o / claude-sonnet-4-20250514.
    system:      Optional system prompt.
    timeout:     HTTP timeout in seconds (default 30).
    base_url:    Custom LLM endpoint e.g. AICredits, OpenRouter.
    auth_token:  Bearer token for MCP servers started with MCPApi(auth_token=...).
    """

    DEFAULTS = {
        "openai":    "gpt-4o",
        "anthropic": "claude-sonnet-4-20250514",
    }

    def __init__(
        self,
        mcp_server:      str | object,
        provider:        Literal["openai", "anthropic"],
        api_key:         str,
        model:           str | None = None,
        system:          str | None = None,
        timeout:         int = 30,
        base_url:        str | None = None,
        default_headers: dict | None = None,
        auth_token:      str | None = None,
    ):
        self.mcp_server      = mcp_server
        self.provider        = provider
        self.api_key         = api_key
        self.model           = model or self.DEFAULTS[provider]
        self.system          = system
        self.timeout         = timeout
        self.base_url        = base_url
        self.default_headers = default_headers
        self.auth_token      = auth_token

    def _transport(self):
        if isinstance(self.mcp_server, str):
            if self.auth_token:
                return StreamableHttpTransport(
                    self.mcp_server,
                    headers={"Authorization": f"Bearer {self.auth_token}"},
                )
            return StreamableHttpTransport(self.mcp_server)
        return self.mcp_server

    async def list_tools(self) -> list[dict]:
        """Fetch available tools from the MCP server."""
        async with _MCPInternalClient(self._transport()) as client:
            tools = await client.list_tools()
        return [
            {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
            for t in tools
        ]

    async def run(self, prompt: str, interpret: bool = True) -> MCPResult:
        """
        Send a prompt, call MCP tools, return MCPResult.

        Parameters
        ----------
        prompt:    The user message.
        interpret: True — LLM explains the result. False — raw tool output only.
        """
        try:
            tools = await self.list_tools()
            if not interpret:
                return await self._run_direct(prompt, tools)
            if self.provider == "openai":
                return await self._run_openai(prompt, tools)
            else:
                return await self._run_anthropic(prompt, tools)
        except Exception as e:
            return MCPResult(answer="", tools_called=[], success=False, error=str(e))

    async def _call_tool(self, name: str, arguments: dict) -> str:
        async with _MCPInternalClient(self._transport()) as client:
            result = await client.call_tool(name, arguments)
        return result.content[0].text if result.content else str(result)

    async def _run_direct(self, prompt: str, mcp_tools: list[dict]) -> MCPResult:
        if self.provider == "openai":
            from openai import AsyncOpenAI
            kw = dict(api_key=self.api_key, timeout=self.timeout)
            if self.base_url:        kw["base_url"]        = self.base_url
            if self.default_headers: kw["default_headers"] = self.default_headers
            client    = AsyncOpenAI(**kw)
            oai_tools = [{"type":"function","function":{"name":t["name"],"description":t.get("description",""),"parameters":t.get("inputSchema",{"type":"object","properties":{}})}} for t in mcp_tools]
            messages: list = []
            if self.system: messages.append({"role":"system","content":self.system})
            messages.append({"role":"user","content":prompt})
            response = await client.chat.completions.create(model=self.model, messages=messages, tools=oai_tools, tool_choice="auto")
            msg = response.choices[0].message
            if not msg.tool_calls:
                return MCPResult(answer=msg.content or "", tools_called=[])
            tc     = msg.tool_calls[0]
            args   = json.loads(tc.function.arguments)
            result = await self._call_tool(tc.function.name, args)
            return MCPResult(answer=result, tools_called=[ToolCall(name=tc.function.name, arguments=args, result=result)])
        else:
            import anthropic as sdk
            kw = dict(api_key=self.api_key, timeout=self.timeout)
            if self.base_url: kw["base_url"] = self.base_url
            client    = sdk.AsyncAnthropic(**kw)
            ant_tools = [{"name":t["name"],"description":t.get("description",""),"input_schema":t.get("inputSchema",{"type":"object","properties":{}})} for t in mcp_tools]
            kw2 = dict(model=self.model, max_tokens=256, messages=[{"role":"user","content":prompt}], tools=ant_tools)
            if self.system: kw2["system"] = self.system
            response   = await client.messages.create(**kw2)
            tool_block = next((b for b in response.content if b.type == "tool_use"), None)
            if not tool_block:
                text = " ".join(b.text for b in response.content if b.type == "text")
                return MCPResult(answer=text, tools_called=[], success=True)
            result = await self._call_tool(tool_block.name, tool_block.input)
            return MCPResult(answer=result, tools_called=[ToolCall(name=tool_block.name, arguments=tool_block.input, result=result)])

    async def _run_openai(self, prompt: str, mcp_tools: list[dict]) -> MCPResult:
        from openai import AsyncOpenAI
        kw = dict(api_key=self.api_key, timeout=self.timeout)
        if self.base_url:        kw["base_url"]        = self.base_url
        if self.default_headers: kw["default_headers"] = self.default_headers
        client    = AsyncOpenAI(**kw)
        oai_tools = [{"type":"function","function":{"name":t["name"],"description":t.get("description",""),"parameters":t.get("inputSchema",{"type":"object","properties":{}})}} for t in mcp_tools]
        messages: list = []
        if self.system: messages.append({"role":"system","content":self.system})
        messages.append({"role":"user","content":prompt})
        tool_calls: list[ToolCall] = []
        while True:
            response = await client.chat.completions.create(model=self.model, messages=messages, tools=oai_tools or None, tool_choice="auto" if oai_tools else None)
            msg = response.choices[0].message
            messages.append(msg)
            if msg.tool_calls:
                results = []
                for tc in msg.tool_calls:
                    args   = json.loads(tc.function.arguments)
                    result = await self._call_tool(tc.function.name, args)
                    tool_calls.append(ToolCall(name=tc.function.name, arguments=args, result=result))
                    results.append({"tool_call_id":tc.id,"role":"tool","content":result})
                messages.extend(results)
            else:
                return MCPResult(answer=msg.content or "", tools_called=tool_calls)

    async def _run_anthropic(self, prompt: str, mcp_tools: list[dict]) -> MCPResult:
        import anthropic as sdk
        kw = dict(api_key=self.api_key, timeout=self.timeout)
        if self.base_url: kw["base_url"] = self.base_url
        client    = sdk.AsyncAnthropic(**kw)
        ant_tools = [{"name":t["name"],"description":t.get("description",""),"input_schema":t.get("inputSchema",{"type":"object","properties":{}})} for t in mcp_tools]
        messages: list  = [{"role":"user","content":prompt}]
        tool_calls: list[ToolCall] = []
        while True:
            kw2 = dict(model=self.model, max_tokens=1024, messages=messages)
            if self.system:   kw2["system"] = self.system
            if ant_tools:     kw2["tools"]  = ant_tools
            response = await client.messages.create(**kw2)
            asst_content, tool_blocks = [], []
            for block in response.content:
                if block.type == "text":
                    asst_content.append({"type":"text","text":block.text})
                elif block.type == "tool_use":
                    asst_content.append({"type":"tool_use","id":block.id,"name":block.name,"input":block.input})
                    tool_blocks.append(block)
            messages.append({"role":"assistant","content":asst_content})
            if tool_blocks:
                results = []
                for block in tool_blocks:
                    result = await self._call_tool(block.name, block.input)
                    tool_calls.append(ToolCall(name=block.name, arguments=block.input, result=result))
                    results.append({"type":"tool_result","tool_use_id":block.id,"content":result})
                messages.append({"role":"user","content":results})
            else:
                final = " ".join(b["text"] for b in asst_content if b.get("type") == "text")
                return MCPResult(answer=tool_calls[-1].result if not final and tool_calls else final, tools_called=tool_calls)


# ─────────────────────────────────────────────────────────────────────────────
# QuickClient — sync, simple, auto-downloads files, multi-server
# ─────────────────────────────────────────────────────────────────────────────

class QuickClient:
    """
    Simple sync MCP client — no asyncio, returns str or FileResult.
    Supports single or multiple MCP servers.
    Automatically detects file URLs and downloads them as FileResult.

    Parameters
    ----------
    mcp_server:  URL string, list of URLs, or FastMCP instance.
    provider:    "openai" or "anthropic"
    api_key:     Your LLM API key.
    model:       Model name. Defaults: gpt-4o / claude-sonnet-4-20250514.
    system:      Optional system prompt.
    timeout:     HTTP timeout in seconds (default 30).
    base_url:    Custom LLM endpoint e.g. AICredits, OpenRouter.
    default_headers: Extra HTTP headers for the LLM request.
    auth_token:  Bearer token for MCP servers started with MCPApi(auth_token=...).
    """

    DEFAULTS = {
        "openai":    "gpt-4o",
        "anthropic": "claude-sonnet-4-20250514",
    }

    def __init__(
        self,
        mcp_server:      str | list[str] | object,
        provider:        Literal["openai", "anthropic"],
        api_key:         str,
        model:           str | None = None,
        system:          str | None = None,
        timeout:         int = 30,
        base_url:        str | None = None,
        default_headers: dict | None = None,
        auth_token:      str | None = None,
    ):
        self._servers        = mcp_server if isinstance(mcp_server, list) else [mcp_server]
        self.provider        = provider
        self.api_key         = api_key
        self.model           = model or self.DEFAULTS[provider]
        self.system          = system
        self.timeout         = timeout
        self.base_url        = base_url
        self.default_headers = default_headers
        self.auth_token      = auth_token

    def _transport(self, server):
        if isinstance(server, str):
            if self.auth_token:
                return StreamableHttpTransport(
                    server, headers={"Authorization": f"Bearer {self.auth_token}"}
                )
            return StreamableHttpTransport(server)
        return server

    async def _fetch_all_tools(self) -> tuple[list[dict], dict[str, object]]:
        tools, tool_map = [], {}
        for server in self._servers:
            async with _MCPInternalClient(self._transport(server)) as client:
                server_tools = await client.list_tools()
            for t in server_tools:
                tools.append({"name":t.name,"description":t.description,"inputSchema":t.inputSchema})
                tool_map[t.name] = server
        return tools, tool_map

    async def _call_tool(self, name: str, arguments: dict, tool_map: dict) -> str:
        server = tool_map.get(name, self._servers[0])
        async with _MCPInternalClient(self._transport(server)) as client:
            result = await client.call_tool(name, arguments)
        return result.content[0].text if result.content else str(result)

    async def _run_openai(self, prompt: str) -> str:
        from openai import AsyncOpenAI
        tools, tool_map = await self._fetch_all_tools()
        kw = dict(api_key=self.api_key, timeout=self.timeout)
        if self.base_url:        kw["base_url"]        = self.base_url
        if self.default_headers: kw["default_headers"] = self.default_headers
        client    = AsyncOpenAI(**kw)
        oai_tools = [{"type":"function","function":{"name":t["name"],"description":t.get("description",""),"parameters":t.get("inputSchema",{"type":"object","properties":{}})}} for t in tools]
        messages: list = []
        if self.system: messages.append({"role":"system","content":self.system})
        messages.append({"role":"user","content":prompt})
        while True:
            response = await client.chat.completions.create(model=self.model, messages=messages, tools=oai_tools or None, tool_choice="auto" if oai_tools else None)
            msg = response.choices[0].message
            messages.append(msg)
            if msg.tool_calls:
                results = []
                for tc in msg.tool_calls:
                    args   = json.loads(tc.function.arguments)
                    result = await self._call_tool(tc.function.name, args, tool_map)
                    results.append({"tool_call_id":tc.id,"role":"tool","content":result})
                messages.extend(results)
            else:
                return msg.content or ""

    async def _run_anthropic(self, prompt: str) -> str:
        import anthropic as sdk
        tools, tool_map = await self._fetch_all_tools()
        kw = dict(api_key=self.api_key, timeout=self.timeout)
        if self.base_url: kw["base_url"] = self.base_url
        client    = sdk.AsyncAnthropic(**kw)
        ant_tools = [{"name":t["name"],"description":t.get("description",""),"input_schema":t.get("inputSchema",{"type":"object","properties":{}})} for t in tools]
        messages: list = [{"role":"user","content":prompt}]
        while True:
            kw2 = dict(model=self.model, max_tokens=1024, messages=messages)
            if self.system: kw2["system"] = self.system
            if ant_tools:   kw2["tools"]  = ant_tools
            response = await client.messages.create(**kw2)
            asst_content, tool_blocks = [], []
            for block in response.content:
                if block.type == "text":
                    asst_content.append({"type":"text","text":block.text})
                elif block.type == "tool_use":
                    asst_content.append({"type":"tool_use","id":block.id,"name":block.name,"input":block.input})
                    tool_blocks.append(block)
            messages.append({"role":"assistant","content":asst_content})
            if tool_blocks:
                results = []
                for block in tool_blocks:
                    result = await self._call_tool(block.name, block.input, tool_map)
                    results.append({"type":"tool_result","tool_use_id":block.id,"content":result})
                messages.append({"role":"user","content":results})
            else:
                return " ".join(b["text"] for b in asst_content if b.get("type") == "text")

    def run(self, prompt: str) -> str | FileResult:
        """
        Send a prompt, call tools, return the result.

        Returns
        -------
        FileResult  — if the tool returned a file (auto-downloaded).
        str         — plain text answer for everything else.
        """
        try:
            answer = asyncio.run(self._run_openai(prompt) if self.provider == "openai" else self._run_anthropic(prompt))
        except Exception as e:
            raise RuntimeError(f"[simcpi] Client error: {e}") from e
        file_info = _extract_file_url(answer)
        if file_info:
            url, filename = file_info
            return FileResult(url=url, filename=filename, data=_download(url))
        return answer

    def tools(self) -> dict[str, list[str]]:
        """Return all available tools grouped by server URL."""
        async def _fetch():
            result = {}
            for server in self._servers:
                async with _MCPInternalClient(self._transport(server)) as client:
                    server_tools = await client.list_tools()
                result[server if isinstance(server, str) else repr(server)] = [t.name for t in server_tools]
            return result
        return asyncio.run(_fetch())
