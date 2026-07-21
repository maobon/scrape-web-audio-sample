import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from news_spider.config import load_config


app = FastAPI()
app.add_middleware(
    CORSMiddleware,  # type: ignore
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
CONFIG = load_config()
NEWS_FILE = Path(CONFIG["storage"]["default_output"])


@app.get("/news")
def get_news():
    with NEWS_FILE.open("r", encoding="utf-8") as file:
        news = json.load(file)

    return {"news": news}


def run() -> None:
    uvicorn.run(
        app,
        host=str(CONFIG["server"]["host"]),
        port=int(CONFIG["server"]["port"]),
    )


if __name__ == "__main__":
    run()
