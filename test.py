import os
from dotenv import load_dotenv
from simcpi import MCPApi
import uvicorn

load_dotenv()

app = MCPApi(
    title="test",
    provider="openai",
    api_key=os.getenv("API_KEY"),
    base_url="https://api.aicredits.in/v1",
    feedback=True,
    model="gpt-4o"
)


@app.create_tool_api("/greet-hindi")
def greet_hindi(name: str) -> str:
    """Greet an individual by their name in Hindi. Use this when you need to provide a personalized greeting in Hindi using a specific person's name. This tool differs from "greet_telugu" by greeting in Hindi instead of Telugu."""
    return f"नमस्ते {name} जी!"


@app.create_tool_api("/greet-telugu")
def greet_telugu(name: str) -> str:
    """Greet the user in Telugu."""
    return f"నమస్కారం {name}!"


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
