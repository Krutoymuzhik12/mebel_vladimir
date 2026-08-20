"""Точка входа: FastAPI webhook Wazzup + фоновые циклы."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.orchestrator import Orchestrator
from app.media_proxy import router as media_router
from app.db.database import Database
from app.services import transcription
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
    wazzup = WazzupTransport(settings, db)
    orch = Orchestrator(settings, db, wazzup)
    app.state.db = db
    app.state.wazzup = wazzup
    app.state.orch = orch
    await orch.startup()
    logger.info(
        "osnova up | push_window=%02d-%02d %s | wazzup_key=%s | poe_key=%s | "
        "max=%s | history=%s | голос=%s | фото=%s",
        settings.push_hour_start,
        settings.push_hour_end,
        settings.timezone,
        "yes" if settings.wazzup_api_key else "НЕТ",
        "yes" if settings.poe_api_key else "НЕТ",
        "on" if settings.max_enabled else "off",
        settings.history_limit,
        transcription.provider(),
        "on" if settings.vision_enabled else "off",
    )
    if not settings.wazzup_send_enabled:
        logger.warning(
            "ОТПРАВКА ВЫКЛЮЧЕНА (WAZZUP_SEND_ENABLED=0): ответы только в лог, "
            "клиентам ничего не уходит"
        )
    if settings.test_mode:
        logger.warning(
            "ТЕСТОВЫЙ РЕЖИМ: слушаю только channel_ids=%s chat_types=%s | отправка=%s",
            sorted(settings.test_channel_id_set) or "—",
            sorted(settings.test_chat_type_set) or "—",
            "вкл" if settings.wazzup_send_enabled else "ВЫКЛ (dry-run)",
        )
    yield
    await orch.shutdown()


app = FastAPI(title="Vladimir mebel osnova", lifespan=lifespan)
app.include_router(media_router)


@app.get("/health")
async def health():
    from app.services import amocrm as amocrm_svc

    return {
        "ok": True,
        "wazzup_configured": bool(settings.wazzup_api_key),
        "poe_configured": bool(settings.poe_api_key),
        "amocrm": amocrm_svc.probe_config(settings),
        "test_mode": settings.test_mode,
        "test_channel_ids": sorted(settings.test_channel_id_set),
        "test_chat_types": sorted(settings.test_chat_type_set),
        "send_enabled": settings.wazzup_send_enabled,
        "max_enabled": settings.max_enabled,
        "history_limit": settings.history_limit,
        "backup_enabled": settings.backup_enabled,
        "health_watch_enabled": settings.health_watch_enabled,
    }


async def _handle_wazzup(
    request: Request,
    headers: dict[str, str],
    path_secret: str | None,
) -> JSONResponse:
    body = await request.body()
    orch: Orchestrator = request.app.state.orch
    if not orch.verify_webhook(headers, body, path_secret):
        logger.warning("wazzup webhook: неверный секрет")
        raise HTTPException(status_code=401, detail="invalid webhook secret")

    try:
        import json

        # errors="replace": одна битая байта не должна терять всё сообщение
        payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
    except Exception:
        logger.warning("wazzup webhook: не JSON, тело=%r", body[:200])
        return JSONResponse({"ok": True})

    if not isinstance(payload, dict):
        logger.warning("wazzup webhook: payload не объект")
        return JSONResponse({"ok": True})

    logger.info("wazzup webhook keys=%s", list(payload.keys()))
    # Wazzup отключает вебхук после серии неответов — отвечаем 200 всегда
    try:
        await orch.handle_webhook_payload(payload)
    except Exception:
        logger.exception("ошибка обработки вебхука")
    return JSONResponse({"ok": True})


@app.post("/webhooks/wazzup")
async def wazzup_webhook(
    request: Request,
    x_wazzup_secret: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    """Входящие события Wazzup. amoCRM в коде не участвует."""
    return await _handle_wazzup(
        request,
        {
            "x-wazzup-secret": x_wazzup_secret or "",
            "x-webhook-secret": x_webhook_secret or "",
            "authorization": authorization or "",
        },
        None,
    )


@app.post("/webhooks/wazzup/{secret}")
async def wazzup_webhook_secret(secret: str, request: Request):
    """Секрет в URL — штатный способ защиты вебхука Wazzup."""
    return await _handle_wazzup(request, {}, secret)


@app.get("/webhooks/wazzup")
@app.get("/webhooks/wazzup/{secret}")
async def wazzup_webhook_probe(secret: str = ""):
    """Wazzup дёргает URL проверкой перед подпиской."""
    _ = secret
    return JSONResponse({"ok": True})


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app", host=settings.app_host, port=settings.app_port, reload=False
    )


if __name__ == "__main__":
    main()
