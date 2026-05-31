from simcpi import QuickClient
import os
from dotenv import load_dotenv

load_dotenv()

client = QuickClient(
    mcp_server="https://myapi.com/mcp/mcp",
    provider="openai",
    api_key=os.getenv("API_KEY"),
    model="gpt-4o",
)

print(client.run("Greet Mohan in Telugu"))
