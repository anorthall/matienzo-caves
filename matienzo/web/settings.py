"""Runtime configuration for the portal.

Read from the environment once, at import, and validated immediately: every
value here is something a container is configured with, and a wrong one should
stop the process at boot rather than surface as a 500 to whoever asks the first
question. That is the whole argument for building this eagerly rather than
lazily per request.

Pydantic rather than `pydantic-settings` because pydantic is already a core
dependency and this is one classmethod. Costs and limits are held as integer
micro-dollars rather than floats — a spend cap is a comparison, and a float
ledger accumulates drift in exactly the direction nobody audits.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from matienzo import config

#: Per-million-token prices for the models the portal will run, in micro-dollars.
#: Cache reads are a tenth of the input price and cache writes a quarter more;
#: both matter here because the tool schemas and corpus primer are cached on
#: every request, so they dominate input tokens.
PRICES_MICROS_PER_MTOK: Final[dict[str, tuple[int, int]]] = {
    "claude-opus-5": (5_000_000, 25_000_000),
    "claude-sonnet-5": (3_000_000, 15_000_000),
    "claude-haiku-4-5": (1_000_000, 5_000_000),
}
CACHE_READ_MULTIPLIER: Final = 0.1
CACHE_WRITE_MULTIPLIER: Final = 1.25

#: Refused because it is the value in the README. An IP salt that everyone who
#: has read the repo knows is not a salt: the hashes become a rainbow table over
#: the 4 billion IPv4 addresses.
PLACEHOLDER_SALT: Final = "change-me"


class Settings(BaseModel):
    """Everything the portal reads from its environment."""

    model_config = ConfigDict(frozen=True)

    db_path: Path = config.DB_PATH
    sessions_db_path: Path = config.SESSIONS_DB_PATH

    api_key: str | None = None
    """Absent means the portal boots permanently in search-only mode. That is a
    supported configuration, not a broken one: the search endpoints are useful
    on their own and are what the whole thing degrades to anyway."""

    model: str = "claude-opus-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    max_tokens: int = Field(default=8192, ge=1024)
    max_tool_iterations: int = Field(default=8, ge=1)
    request_timeout_seconds: float = Field(default=120.0, gt=0)

    daily_cap_micros: int = Field(default=5_000_000, ge=0)
    max_concurrent_streams: int = Field(default=6, ge=1)
    """Bounds how far past the daily cap concurrent streams can take us. Usage is
    unknown until a turn finishes, so the reservation is pessimistic and the
    residual overshoot is at most this many turns' worth — a stated number rather
    than a pretence that the cap is exact."""

    rate_per_min: float = Field(default=6.0, gt=0)
    rate_burst: float = Field(default=4.0, ge=1)

    ip_salt: str = PLACEHOLDER_SALT
    trusted_proxy_hops: int = Field(default=1, ge=0)
    """How many proxies of our own sit in front. `X-Forwarded-For` is read by
    index from the *right*: everything to the left of our own hops is whatever
    the client chose to send."""

    cors_origins: tuple[str, ...] = ()
    session_ttl_days: int = Field(default=14, ge=1)
    keepalive_seconds: float = Field(default=15.0, gt=0)
    """Injectable so tests do not wait a quarter of a minute to observe one."""

    embedding_limit: int = Field(default=2, ge=1)
    threadpool_limit: int = Field(default=12, ge=1)

    @property
    def can_answer(self) -> bool:
        """Whether the agent path is available at all."""
        return self.api_key is not None

    @property
    def price_micros_per_mtok(self) -> tuple[int, int]:
        return PRICES_MICROS_PER_MTOK[self.model]

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.model not in PRICES_MICROS_PER_MTOK:
            known = ", ".join(sorted(PRICES_MICROS_PER_MTOK))
            raise ValueError(
                f"unknown model {self.model!r}: no price is recorded for it, so the "
                f"spend cap could not be enforced. Known models: {known}"
            )
        if self.api_key and self.ip_salt == PLACEHOLDER_SALT:
            raise ValueError(
                "MATIENZO_IP_SALT is still the placeholder. Rate limiting hashes "
                "client addresses with it, and a salt anyone can read out of the "
                "repository does not hide an IPv4 address from a rainbow table."
            )
        return self

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        """Build from the environment, leaving unset values at their defaults.

        Takes the mapping as an argument so tests can construct a configuration
        without mutating the process environment.
        """
        env = os.environ if environ is None else environ

        def get(name: str) -> str | None:
            value = env.get(name)
            return value.strip() or None if value else None

        values: dict[str, Any] = {}
        if (raw := get("MATIENZO_DB")) is not None:
            values["db_path"] = Path(raw)
        if (raw := get("MATIENZO_SESSIONS_DB")) is not None:
            values["sessions_db_path"] = Path(raw)
        if (raw := get("ANTHROPIC_API_KEY")) is not None:
            values["api_key"] = raw
        if (raw := get("MATIENZO_MODEL")) is not None:
            values["model"] = raw
        if (raw := get("MATIENZO_EFFORT")) is not None:
            values["effort"] = raw
        if (raw := get("MATIENZO_MAX_TOKENS")) is not None:
            values["max_tokens"] = int(raw)
        if (raw := get("MATIENZO_MAX_TOOL_ITERATIONS")) is not None:
            values["max_tool_iterations"] = int(raw)
        if (raw := get("MATIENZO_DAILY_CAP_USD")) is not None:
            values["daily_cap_micros"] = round(float(raw) * 1_000_000)
        if (raw := get("MATIENZO_MAX_CONCURRENT_STREAMS")) is not None:
            values["max_concurrent_streams"] = int(raw)
        if (raw := get("MATIENZO_RATE_PER_MIN")) is not None:
            values["rate_per_min"] = float(raw)
        if (raw := get("MATIENZO_RATE_BURST")) is not None:
            values["rate_burst"] = float(raw)
        if (raw := get("MATIENZO_IP_SALT")) is not None:
            values["ip_salt"] = raw
        if (raw := get("MATIENZO_TRUSTED_PROXY_HOPS")) is not None:
            values["trusted_proxy_hops"] = int(raw)
        if (raw := get("MATIENZO_CORS_ORIGINS")) is not None:
            values["cors_origins"] = tuple(o.strip() for o in raw.split(",") if o.strip())
        if (raw := get("MATIENZO_SESSION_TTL_DAYS")) is not None:
            values["session_ttl_days"] = int(raw)

        return cls(**values)
