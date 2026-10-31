from fastapi import FastAPI

app = FastAPI(title="EventLedger")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

