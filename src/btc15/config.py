"""Settings, startup validation and the params_version hash."""
from __future__ import annotations

import hashlib
import json
from typing import Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import spec


class ConfigError(Exception):
    """Raised when settings make it unsafe to start."""


# Fields that define the *strategy*; changing any of them starts a fresh proof record.
# Secrets, paths, mode and risk limits are deliberately excluded so that PAPER evidence
# gathered under the same strategy carries over, and loosening/tightening a risk limit
# does not silently reset (or fake) the proof.
STRATEGY_FIELDS = (
    "SETTLE_WINDOW_SEC", "VOL_SOURCE", "REQUIRE_3_FEEDS", "BASIS_METHOD",
    "FEE_RATE", "KELLY_FRACTION", "MAX_STAKE_FRAC", "MIN_EDGE", "EDGE_SUSPECT",
    "MAX_DIVERGENCE", "ENTRY_MIN_SEC", "ENTRY_MAX_SEC", "SPREAD_MIN", "SPREAD_MAX",
    "VOL_MIN", "VOL_MAX", "MAX_GAP_PCT", "REF_MAX_AGE_SEC", "QUOTE_MAX_AGE_SEC",
    "BASIS_HALF_LIFE_MARKETS",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- mode / secrets ---
    MODE: Literal["PAPER", "DEMO", "LIVE"] = "PAPER"
    KALSHI_KEY_ID: Optional[str] = None
    KALSHI_PRIVATE_KEY_PATH: Optional[str] = None
    DASHBOARD_USER: str = "admin"
    DASHBOARD_PASSWORD: Optional[str] = None
    DASHBOARD_HOST: str = "0.0.0.0"
    DASHBOARD_PORT: int = 8080

    # --- LIVE guards ---
    LIVE_CONFIRM: Optional[str] = None
    ALLOW_UNPROVEN: Optional[str] = None
    DAILY_LOSS_LIMIT_USD: Optional[float] = None
    MAX_TOTAL_LOSS_USD: Optional[float] = None
    MAX_CONTRACTS_PER_TRADE: Optional[int] = None

    # --- alerts (secret: anyone with the URL can post to the channel) ---
    DISCORD_WEBHOOK_URL: Optional[str] = None

    # --- bankroll / storage ---
    PAPER_BANKROLL: float = 1000.0
    DB_PATH: str = "btc15.db"
    KILL_FILE: str = "KILL"

    # --- strategy (hashed into params_version) ---
    SETTLE_WINDOW_SEC: int = spec.SETTLE_WINDOW_SEC_DEFAULT
    VOL_SOURCE: Literal["blend", "realized"] = "blend"
    REQUIRE_3_FEEDS: bool = True
    BASIS_METHOD: Literal["avg60", "spot"] = "avg60"
    BASIS_HALF_LIFE_MARKETS: float = 8.0
    FEE_RATE: float = spec.FEE_RATE
    KELLY_FRACTION: float = 0.25
    MAX_STAKE_FRAC: float = 0.02
    MIN_EDGE: float = 0.05
    EDGE_SUSPECT: float = 0.15
    MAX_DIVERGENCE: float = 0.12
    ENTRY_MIN_SEC: float = 180.0
    ENTRY_MAX_SEC: float = 600.0
    SPREAD_MIN: float = 0.97
    SPREAD_MAX: float = 1.12
    VOL_MIN: float = 0.03
    VOL_MAX: float = 2.50
    MAX_GAP_PCT: float = 0.10
    REF_MAX_AGE_SEC: float = 2.0
    QUOTE_MAX_AGE_SEC: float = 3.0

    # --- execution ---
    ORDER_TIMEOUT_SEC: float = 10.0
    MAX_API_ERRORS: int = 3

    @property
    def env(self) -> str:
        return "PROD" if self.MODE == "LIVE" else "DEMO"

    @property
    def rest_base(self) -> str:
        """DEMO and LIVE use their own environment for BOTH quotes and orders (tickers must match).
        PAPER reads production market data and never sends orders."""
        return spec.REST_BASE["DEMO" if self.MODE == "DEMO" else "PROD"]

    def params_version(self) -> str:
        blob = json.dumps({k: getattr(self, k) for k in STRATEGY_FIELDS}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def effective_max_contracts(self, proof_status: str) -> int:
        """Per-trade contract cap. LIVE + UNPROVEN is forced to 1 regardless of config."""
        cfg = self.MAX_CONTRACTS_PER_TRADE if self.MAX_CONTRACTS_PER_TRADE is not None else 1
        if self.MODE == "LIVE" and proof_status != "PROVEN":
            return 1
        return max(0, cfg)


def validate_for_start(s: Settings, proof_status: str) -> None:
    """Refuse to start on unsafe or incomplete configuration. Raises ConfigError."""
    problems: list[str] = []
    if not s.DASHBOARD_PASSWORD or len(s.DASHBOARD_PASSWORD) < 8:
        problems.append("DASHBOARD_PASSWORD must be set (>= 8 chars)")
    if s.MODE in ("DEMO", "LIVE"):
        if not s.KALSHI_KEY_ID:
            problems.append("KALSHI_KEY_ID is required")
        if not s.KALSHI_PRIVATE_KEY_PATH:
            problems.append("KALSHI_PRIVATE_KEY_PATH is required")
    if s.MODE == "LIVE":
        if s.LIVE_CONFIRM != "yes":
            problems.append("LIVE_CONFIRM=yes is required")
        for name in ("DAILY_LOSS_LIMIT_USD", "MAX_TOTAL_LOSS_USD", "MAX_CONTRACTS_PER_TRADE"):
            v = getattr(s, name)
            if v is None or v <= 0:
                problems.append(f"{name} must be set to a positive value")
        if proof_status != "PROVEN" and s.ALLOW_UNPROVEN != "yes":
            problems.append("proof status is UNPROVEN: ALLOW_UNPROVEN=yes is required for LIVE")
    if s.DISCORD_WEBHOOK_URL:
        from .notify import valid_webhook
        if not valid_webhook(s.DISCORD_WEBHOOK_URL):
            problems.append("DISCORD_WEBHOOK_URL must be a https://discord.com/api/webhooks/... URL")
    if s.SETTLE_WINDOW_SEC <= 0:
        problems.append("SETTLE_WINDOW_SEC must be positive")
    if problems:
        raise ConfigError("Refusing to start: " + "; ".join(problems))
