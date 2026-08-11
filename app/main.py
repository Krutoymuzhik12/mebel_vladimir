"""Точка входа: FastAPI webhook Wazzup + фоновые циклы."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.orchestrator import Orchestrator
from app.db.database import Database
from app.transports.wazzup import WazzupTransport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("osnova")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_file)
    wazzup = WazzupTransport(settings)
    orch = Orchestrator(settings, db, wazzup)
    app.state.db = db
    app.state.wazzup = wazzup
    app.state.orch = orch
    await orch.startup()
    logger.info(
        "osnova up | push_window=%02d-%02d %s | wazzup_key=%s | max=%s | history=%s",
        settings.push_hour_start,
        settings.push_hour_end,
        settings.timezone,
        "yes" if settings.wazzup_api_key else "no",
        "on" if settings.max_enabled else "off",
        settings.history_limit,
    )
    yield
    await orch.shutdown()


app = FastAPI(title="Vladimir mebel osnova", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "wazzup_configured": bool(settings.wazzup_api_key),
        "max_enabled": settings.max_enabled,
        "history_limit": settings.history_limit,
    }


@app.post("/webhooks/wazzup")
async def wazzup_webhook(
    request: Request,
    x_wazzup_secret: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    """Входящие события Wazzup. amoCRM в коде не участвует."""
    body = await request.body()
    headers = {
        "x-wazzup-secret": x_wazzup_secret or "",
        "x-webhook-secret": x_webhook_secret or "",
        "authorization": authorization or "",
    }
    orch: Orchestrator = request.app.state.orch
    if not orch.verify_webhook(headers, body):
        raise HTTPException(status_code=401, detail="invalid webhook secret")

    try:
        import json

        payload = json.loads(body.decode("utf-8") or "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be object")

    logger.info("wazzup webhook keys=%s", list(payload.keys()))
    await orch.handle_webhook_payload(payload)
    return JSONResponse({"ok": True})


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, reload=False)


if __name__ == "__main__":
    main()
