"""FastAPI entry point for burnrate credit card analytics backend."""

import logging
import os
import sys
from pathlib import Path

# Ensure project root is in path for backend imports
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from backend.models.database import SessionLocal, init_db
from backend.models.models import CategoryDefinition, Settings
from backend.config import settings
from backend.routers import analytics, cards, categories, data, due_reminders, settings as backend_settings, statements, tags, transactions

logger = logging.getLogger(__name__)


def seed_categories(db) -> None:
    """Seed prebuilt categories if not present."""
    from backend.constants import PREBUILT_CATEGORIES
    for cat_data in PREBUILT_CATEGORIES:
        existing = db.query(CategoryDefinition).filter(CategoryDefinition.slug == cat_data["slug"]).first()
        if not existing:
            db.add(CategoryDefinition(is_prebuilt=1, **cat_data))
        elif existing.is_prebuilt and existing.keywords != cat_data["keywords"]:
            existing.keywords = cat_data["keywords"]
    db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    import threading

    init_db()

    db = SessionLocal()
    watch_folder = None
    try:
        seed_categories(db)
        s = db.query(Settings).first()
        if s and s.watch_folder:
            watch_folder = s.watch_folder
        else:
            logger.info("No watch_folder configured, skipping folder watcher")
    finally:
        db.close()

    # Start folder watcher in a background thread so it doesn't block startup.
    # On macOS, accessing TCC-protected directories (~/Documents etc.) triggers
    # a system permission dialog that would otherwise block until the user responds,
    # preventing "Application startup complete" from firing and keeping the splash
    # screen visible indefinitely.
    if watch_folder:
        def _start_watcher_deferred() -> None:
            from backend.services.folder_watcher import start_watcher
            from backend.routers.settings import set_watcher_observer

            try:
                observer = start_watcher(watch_folder, db_session_factory=SessionLocal)
                if observer:
                    set_watcher_observer(observer)
                    logger.info("Folder watcher started on %s", watch_folder)
                else:
                    logger.warning("Failed to start folder watcher for %s", watch_folder)
            except Exception:
                logger.exception("Error starting folder watcher")

        threading.Thread(
            target=_start_watcher_deferred,
            name="watcher-init",
            daemon=True,
        ).start()

    # Browser auto-open for Homebrew distribution only
    def _open_browser_on_startup() -> None:
        """Open browser when server starts up (Homebrew only)."""
        import time
        import webbrowser

        # Only open browser if running from Homebrew
        if settings.burnrate_homebrew != "true":
            return

        port = settings.burnrate_port
        time.sleep(2)  # Wait for server startup
        try:
            webbrowser.open(f"http://localhost:{port}")
            logger.info("Browser opened at http://localhost:%s", port)
        except Exception:
            logger.exception("Failed to open browser")

    threading.Thread(target=_open_browser_on_startup, name="browser-opener", daemon=True).start()

    yield

    from backend.routers.settings import get_watcher_observer
    from backend.services.folder_watcher import stop_watcher
    from backend.services import processing_queue

    observer = get_watcher_observer()
    if observer:
        stop_watcher(observer)
    processing_queue.shutdown(wait=True)


app = FastAPI(title="Burnrate Credit Card Analytics", lifespan=lifespan)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self';"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# The API binds to loopback, but any web page can still reach it. Reject
# foreign Host headers so a DNS-rebinding page cannot become same-origin.
from starlette.middleware.trustedhost import TrustedHostMiddleware

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1", "::1", "tauri.localhost", "testserver"],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:6006",
        "http://localhost:6007",
        "tauri://localhost",
        "https://tauri.localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(backend_settings.router, prefix="/api")
app.include_router(cards.router, prefix="/api")
app.include_router(due_reminders.router, prefix="/api")
app.include_router(statements.router, prefix="/api")
app.include_router(transactions.router, prefix="/api")
app.include_router(analytics.router, prefix="/api")
app.include_router(categories.router, prefix="/api")
app.include_router(tags.router, prefix="/api")
app.include_router(data.router, prefix="/api")

_project_root_for_static = Path(__file__).resolve().parent.parent
_static_candidates = [
    settings.burnrate_static_dir,
    str(_project_root_for_static / "frontend-neopop" / "dist"),
    str(_project_root_for_static / "frontend" / "dist"),
]
_static_dir: Optional[str] = None
for _candidate in _static_candidates:
    if _candidate and Path(_candidate).is_dir():
        _static_dir = _candidate
        app.mount("/assets", StaticFiles(directory=str(Path(_candidate) / "assets")), name="static-assets")
        logger.info("Serving static files from %s", _candidate)
        break

if _static_dir:
    from starlette.responses import FileResponse

    _index_html = Path(_static_dir) / "index.html"

    _static_root_resolved = Path(_static_dir).resolve()

    @app.get("/{full_path:path}")
    async def _spa_fallback(full_path: str):
        """Serve index.html for all non-API, non-asset routes (SPA routing)."""
        if ".." in full_path.split("/"):
            return FileResponse(_index_html)
        requested = (_static_root_resolved / full_path).resolve()
        if (
            requested.is_file()
            and requested.is_relative_to(_static_root_resolved)
        ):
            return FileResponse(str(requested))
        return FileResponse(_index_html)


if __name__ == "__main__":
    import uvicorn
    port = settings.burnrate_port
    uvicorn.run(app, host="127.0.0.1", port=port)
