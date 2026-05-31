import os
from dotenv import load_dotenv
from simcpi import MCPApi
import uvicorn

load_dotenv()

app = MCPApi(
    title="test3",
    provider="openai",
    api_key=os.getenv("API_KEY"),
    base_url="https://api.aicredits.in/v1",
    model="gpt-4o"
)


@app.create_tool_api("/generate-image")
def generate_image(prompt: str) -> str:
    """
    Generate an image using DALL-E based on the prompt and return it.
    Use this when the user asks to create, draw, or generate any image.
    """
    from openai import OpenAI

    client = OpenAI(
        api_key=os.getenv("API_KEY"),
        base_url="https://api.aicredits.in/v1",
    )

    import base64

    response = client.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        n=1,
        size="1024x1024",
    )

    image_bytes = base64.b64decode(response.data[0].b64_json)
    return app.serve_file("generated_image.png", image_bytes, port=8002)


# ── Concept example: serving an already existing image file ──────────────────
# No conversion needed — serve_file just hosts the file and returns the URL.
#
# @app.create_tool_api("/generate-preimage")
# def generate_preimage() -> str:
#     """Return a pre-existing image file directly."""
#     return app.serve_file("photo.png", "/path/to/photo.png", port=8002)
# ─────────────────────────────────────────────────────────────────────────────


@app.create_tool_api("/sales-report")
def sales_report() -> str:
    """
    Generate a sales report as an Excel file.
    Use this when the user asks for a sales report or data export.
    """
    import pandas as pd

    df = pd.DataFrame({
        "Month":    ["Jan", "Feb", "Mar", "Apr", "May", "Jun"],
        "Sales":    [12400, 15800, 13200, 17600, 19100, 22300],
        "Expenses": [8200,  9400,  8900,  10200, 11300, 12800],
        "Profit":   [4200,  6400,  4300,  7400,  7800,  9500],
    })
    return app.serve_file("sales_report.xlsx", df, port=8002)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8002)
