"""Write policy checked by both the service facade and the HTTP adapter."""
from .config import Settings
from .errors import OmniError


def require_write(settings: Settings, robot_id: str) -> None:
    if settings.backend != "http":
        raise OmniError("unsupported_backend", "Robot operations require the HTTP backend; SSH is read-only")
    if not settings.allow_writes:
        raise OmniError("writes_disabled", "Execution is disabled; OMNI_ALLOW_WRITES must be 1")
    if robot_id not in settings.allowed_robots:
        raise OmniError("robot_not_allowed", "Robot is not in OMNI_ALLOWED_ROBOTS")
