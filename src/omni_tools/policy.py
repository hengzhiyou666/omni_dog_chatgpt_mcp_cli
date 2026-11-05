"""Write gates shared by the service facade and execution adapters."""
from .config import Settings
from .errors import OmniError


def require_write(settings: Settings, robot_id: str) -> None:
    if settings.backend != "http":
        raise OmniError("unsupported_backend", "Mission operations require the HTTP backend")
    _require_enabled(settings, robot_id)


def require_posture_write(settings: Settings, robot_id: str) -> None:
    """Fixed posture actions require SSH or local ROS and explicit write policy."""
    if settings.backend not in {"ssh", "local"}:
        raise OmniError("unsupported_backend", "Posture operations require SSH or local ROS")
    _require_enabled(settings, robot_id)


def require_motion_write(settings: Settings, robot_id: str) -> None:
    """Bounded robot-side motion requires an explicitly enabled ROS target."""
    if settings.backend not in {"ssh", "local"}:
        raise OmniError("unsupported_backend", "Bounded motion requires SSH or local ROS")
    _require_enabled(settings, robot_id)


def require_runtime_write(settings: Settings, robot_id: str) -> None:
    if settings.backend not in {"ssh", "local"}:
        raise OmniError("unsupported_backend", "Runtime operations require SSH or local ROS")
    _require_enabled(settings, robot_id)


def _require_enabled(settings: Settings, robot_id: str) -> None:
    if not settings.allow_writes:
        raise OmniError("writes_disabled", "Execution is disabled; OMNI_ALLOW_WRITES must be 1")
    if robot_id not in settings.allowed_robots:
        raise OmniError("robot_not_allowed", "Robot is not in OMNI_ALLOWED_ROBOTS")
