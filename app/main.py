from fastapi import FastAPI, Depends
from app.config import Settings
from app.dependencies import get_settings_dep

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}
