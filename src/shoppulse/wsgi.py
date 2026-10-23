"""WSGI entry point for gunicorn:  gunicorn shoppulse.wsgi:app"""

from shoppulse.api import create_app
from shoppulse.config import get_settings
from shoppulse.logging_setup import configure_logging

_settings = get_settings()
configure_logging(_settings.logs_dir, _settings.log_level)
app = create_app(_settings)
