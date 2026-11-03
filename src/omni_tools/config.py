"""Configuration is explicit; credentials are read only from the environment."""
import math
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .errors import OmniError
from .util import identifier


@dataclass(frozen=True)
class Settings:
    backend: str = "ssh"
    robot_id: str = "dog4"
    ssh_host: str = "dog4"
    timeout: float = 12.0
    ssh_connect_timeout: int = 10
    base_url: str = ""
    token: str = field(default="", repr=False)

    def __post_init__(self):
        if self.backend not in {"ssh", "http"}:
            raise OmniError("configuration_error", "OMNI_BACKEND must be ssh or http")
        identifier(self.robot_id, "robot_id")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,252}", self.ssh_host):
            raise OmniError("configuration_error", "Invalid SSH host alias")
        if not math.isfinite(self.timeout) or not 1 <= self.timeout <= 120:
            raise OmniError("configuration_error", "Timeout must be between 1 and 120 seconds")
        if not 1 <= self.ssh_connect_timeout <= 30:
            raise OmniError("configuration_error", "SSH connection timeout must be 1..30 seconds")
        if self.backend == "http":
            url = urlsplit(self.base_url)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username or url.password or url.query or url.fragment):
                raise OmniError("configuration_error", "Set OMNI_BASE_URL to a server URL without credentials/query")
            if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise OmniError("configuration_error", "Remote HTTP backend requires HTTPS")
            if not self.token or "\n" in self.token or "\r" in self.token:
                raise OmniError("configuration_error", "Set OMNI_TOKEN to the backend Bearer token")

    @classmethod
    def from_env(cls, **overrides):
        try:
            values = dict(backend=os.getenv("OMNI_BACKEND", "ssh"),
                          robot_id=os.getenv("OMNI_ROBOT_ID", "dog4"),
                          ssh_host=os.getenv("OMNI_SSH_HOST", "dog4"),
                          timeout=float(os.getenv("OMNI_TIMEOUT", "12")),
                          ssh_connect_timeout=int(os.getenv("OMNI_SSH_CONNECT_TIMEOUT", "10")),
                          base_url=os.getenv("OMNI_BASE_URL", ""),
                          token=os.getenv("OMNI_TOKEN", ""))
            values.update({k: v for k, v in overrides.items() if v is not None})
            return cls(**values)
        except (ValueError, TypeError):
            raise OmniError("configuration_error", "Invalid numeric configuration") from None
