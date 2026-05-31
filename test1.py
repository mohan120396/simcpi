import os
from dotenv import load_dotenv
from simcpi import MCPApi
import uvicorn

load_dotenv()

app = MCPApi(
    title="test1",
    provider="openai",
    api_key=os.getenv("API_KEY"),
    base_url="https://api.aicredits.in/v1",
    model="gpt-4o"
)


@app.create_tool_api("/greet-softly")
def greet_softly(name: str) -> str:
    """
    Greet the user softspokenly.
    """
    return f"greeting {name} respectfully!"


@app.create_tool_api("/greet-rude")
def greet_rude(name: str) -> str:
    """
    Greet the user rudely.
    """
    return f"greeting {name} rudely!"
    

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
    