from fastapi import FastAPI

from app.api import (
    media_router,
    pipeline_router,
    review_router,
    sources_router,
    summaries_router,
)

app = FastAPI(title="finvid-pipeline")

app.include_router(review_router)
app.include_router(summaries_router)
app.include_router(pipeline_router)
app.include_router(media_router)
app.include_router(sources_router)


@app.get("/health")
def health():
    return {"status": "ok"}
