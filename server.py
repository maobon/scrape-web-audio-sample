import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
NEWS_FILE = Path(__file__).with_name("news_data.json")


@app.get("/news")
def get_news():
    with NEWS_FILE.open("r", encoding="utf-8") as file:
        news = json.load(file)

    return {"news": news}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
