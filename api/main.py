from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.rest import router as rest_router
from api.web import router as web_router


app = FastAPI(
    title="Mock MES REST API",
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(rest_router)
app.include_router(web_router)
