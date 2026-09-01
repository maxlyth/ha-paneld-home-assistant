"""Constants for the ha-paneld integration."""

from datetime import timedelta

DOMAIN = "ha_paneld"

DEFAULT_PORT = 8888
DEFAULT_SCAN_INTERVAL = timedelta(seconds=30)
DEFAULT_TIMEOUT_SECONDS = 5
MAX_HEALTH_RESPONSE_BYTES = 512

HEALTH_PATH = "/api/v1/health"
