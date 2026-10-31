from fastapi import FastAPI

from app.api.v1.router import router as v1_router

app = FastAPI(title="EventLedger")
app.include_router(v1_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

