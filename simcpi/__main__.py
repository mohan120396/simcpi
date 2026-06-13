"""
MCPark as a standalone MCP inspector — point it at ANY MCP server:

    python -m simcpi https://myapi.com/mcp/mcp
    python -m simcpi http://127.0.0.1:9000/mcp/mcp --auth my-token --port 8123
    mcpark https://myapi.com/mcp/mcp            (same thing, via the console script)

Opens MCPark pre-connected to the remote server: explore its tools, run
LLM-in-the-loop tests, preview returned files and images. No remote URL —
opens an empty MCPark you can connect from the UI.
"""

import argparse
import os


def main() -> None:
    p = argparse.ArgumentParser(
        prog="mcpark",
        description="MCPark — explore and LLM-test any MCP server in the browser.",
    )
    p.add_argument("url", nargs="?", default=None,
                   help="MCP server URL, e.g. https://host/mcp/mcp")
    p.add_argument("--auth", default=None,
                   help="Bearer token for the remote MCP server (if it needs one)")
    p.add_argument("--port", type=int, default=8123, help="local UI port (default 8123)")
    p.add_argument("--provider", choices=["openai", "anthropic"], default=None,
                   help="LLM provider for the tester (or set in the UI)")
    p.add_argument("--api-key", default=None,
                   help="LLM API key (falls back to OPENAI_API_KEY / ANTHROPIC_API_KEY)")
    p.add_argument("--model", default=None, help="LLM model name")
    p.add_argument("--base-url", default=None, help="LLM base URL (proxies/gateways)")
    args = p.parse_args()

    from .mcpserver import MCPApi
    import uvicorn

    api_key = args.api_key or os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    provider = args.provider or ("openai" if os.getenv("OPENAI_API_KEY") or not
                                 os.getenv("ANTHROPIC_API_KEY") else "anthropic")

    app = MCPApi(
        title="MCPark Inspector",
        provider=provider if api_key else None,
        api_key=api_key,
        model=args.model,
        base_url=args.base_url,
    )
    if args.url:
        app.default_remote = {"url": args.url, "auth": args.auth}
        print(f"[mcpark] inspecting remote MCP server: {args.url}")

    print(f"[mcpark] open  http://127.0.0.1:{args.port}/mcpark")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
