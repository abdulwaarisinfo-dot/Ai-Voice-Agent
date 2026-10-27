import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openai import AsyncOpenAI
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings, get_settings
from .database import connect, init_indexes
from .money import format_money
from .routers import menu, orders, pages, voice
from .security import RateLimiter
from .services.live_session import LiveService

BASE = Path(__file__).parent
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def create_app(settings: Settings | None = None, db=None, live_client=None, sideband_connect=None) -> FastAPI:
    """Factory so tests can inject a mongomock database and a fake OpenAI client."""
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO)
    secret = settings.effective_secret()
    if db is None:
        db = connect(settings)
        init_indexes(db)
    app = FastAPI(title="Restaurant Voice Agent", docs_url=None if settings.is_production else "/docs", redoc_url=None)
    app.state.settings, app.state.db, app.state.secret = settings, db, secret
    client = live_client or AsyncOpenAI(api_key=settings.openai_api_key or "not-configured", max_retries=0, timeout=30)
    kwargs = {"connect": sideband_connect} if sideband_connect else {}
    app.state.live = LiveService(db, settings, client, **kwargs)
    app.state.call_limiter = RateLimiter(5, 600)       # new calls per IP per 10 min
    app.state.login_limiter = RateLimiter(10, 300)
    app.state.status_limiter = RateLimiter(240, 60)    # heartbeat polling every 2s = 30/min
    templates = Jinja2Templates(directory=str(BASE / "templates"))
    templates.env.filters["money"] = format_money
    app.state.templates = templates

    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="strict", https_only=settings.is_production,
                       max_age=8 * 3600, session_cookie="owner_session")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")  # order links carry a token
        response.headers.setdefault("Permissions-Policy", "microphone=(self)")
        if request.url.path.startswith(("/api/", "/confirmation/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
    for r in (pages.router, menu.router, voice.router, orders.router):
        app.include_router(r)
    return app


def app_factory() -> FastAPI:  # uvicorn app.main:app_factory --factory
    return create_app()
