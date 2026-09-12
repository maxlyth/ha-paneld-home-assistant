"""Constants for the ha-paneld integration."""

from datetime import timedelta

DOMAIN = "panel_assistant"
# The public version (manifest.json) only changes when something ships. This
# build number tells builds apart in between: it counts the commits that have
# changed this integration.
INTEGRATION_BUILD = 36

DEFAULT_PORT = 8888
DEFAULT_SCAN_INTERVAL = timedelta(seconds=30)
DEFAULT_TIMEOUT_SECONDS = 5
MAX_HEALTH_RESPONSE_BYTES = 512
MAX_STATUS_RESPONSE_BYTES = 64 * 1024
MAX_INSTALL_RESPONSE_BYTES = 1024
MAX_STATUS_WARNINGS = 32
MAX_STATUS_CAPABILITIES = 32
MAX_STATUS_REASON_CODES = 32
MAX_STATUS_WARNING_LENGTH = 2048
MAX_STATUS_TOKEN_LENGTH = 128
MAX_STATUS_FAULT_DETAIL_LENGTH = 40
MAX_STATUS_INTEGER = 2**63 - 1
MIN_ANDROID_INTEGER = -(2**31)
MAX_ANDROID_INTEGER = 2**31 - 1
MAX_ZIGBEE_CPU_PERCENT = 1000

HEALTH_PATH = "/api/v1/health"
STATUS_PATH = "/api/v1/status"
# Tells the panel this Home Assistant shows its ha-paneld update, so the panel
# withholds its own MQTT update entity rather than duplicating it.
UPDATE_OWNER_HEADER = "X-Panel-Assistant-Update-Owner"
INSTALL_COMPONENT_PATH = "/api/v1/install/component"
INSTALL_STATUS_PATH = "/api/v1/install/status"
APK_STAGE_PATH = "/api/v1/install/apk"
APK_COMMIT_PATH = "/api/v1/install/apk/commit"
APK_DISCARD_PATH = "/api/v1/install/apk/discard"
BACKUP_PATH = "/api/v1/backup"
DIAG_PATH = "/api/v1/diag"
SETUP_PATH = "/api/v1/setup"


def update_unique_id(entry_id: str) -> str:
    """Return the registry unique ID of an entry's ha-paneld update entity."""
    return f"{entry_id}_update"
