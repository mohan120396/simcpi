from simcpi import QuickClient
import os
from dotenv import load_dotenv

load_dotenv()

client = QuickClient(
    mcp_server="http://localhost:8000/mcp/mcp",
    provider="openai",
    api_key=os.getenv("API_KEY"),
    base_url="https://api.aicredits.in/v1",
    model="gpt-4o",
)

print(client.run("Greet Mohan in Hindi"))
