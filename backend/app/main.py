from fastapi import FastAPI

from app.api import review_router

app = FastAPI(title="finvid-pipeline")

app.include_router(review_router)


@app.get("/health")
def health():
    return {"status": "ok"}
