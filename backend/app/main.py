from fastapi import FastAPI

app = FastAPI(title="finvid-pipeline")


@app.get("/health")
def health():
    return {"status": "ok"}
