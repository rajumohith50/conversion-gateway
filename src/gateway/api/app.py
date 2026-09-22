"""Application factory.

create_app() takes its dependencies as arguments and stores them on
app.state. Routes read them back from `request.app.state`. That is the
whole dependency-injection story: explicit construction, no framework,
and tests build an app with test settings and a test database in two lines.
"""

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from gateway.api.routes import router
from gateway.config import Settings
from gateway.db import make_engine, make_session_factory
from gateway.observability import configure_logging
from gateway.queue import QueuePublisher
from gateway.wiring import make_publisher


def create_app(
    settings: Settings, session_factory: sessionmaker[Session], publisher: QueuePublisher
) -> FastAPI:
    app = FastAPI(title="Conversion Gateway", version="0.1.0")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.publisher = publisher
    app.include_router(router)
    return app


def create_app_from_env() -> FastAPI:
    """Entry point for `uvicorn gateway.api.app:create_app_from_env --factory`."""
    settings = Settings()
    configure_logging(settings.log_level)
    session_factory = make_session_factory(make_engine(settings.database_url))
    return create_app(settings, session_factory, make_publisher(settings))
