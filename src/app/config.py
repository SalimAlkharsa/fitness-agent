"""Runtime configuration, loaded from environment (.env in dev).

Read once, exposed as a module-level `settings` object. Nothing here raises on
import — missing secrets are surfaced by the components that actually need them,
so tests and `make lint` run without a populated .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in _TRUTHY


def _opt(name: str) -> str | None:
    """Optional secret. `unset` counts as absent: project.yml's substituter
    rejects empty values, so `make deploy` passes that placeholder instead."""
    v = (os.getenv(name) or "").strip()
    return None if v in ("", "unset") else v


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str | None
    google_sa_json: str | None
    sheet_id: str | None
    passphrase: str | None
    secret_key: str | None
    model: str
    fallback_model: str | None
    tool_mode: str
    dry_run: bool
    # URL prefix the app is served under ("" locally; the function's
    # /api/v1/web/<ns>/<pkg>/<fn> path on DO Functions). Templates and redirects
    # prefix every link with it.
    base_path: str
    # DO Functions self-invoke creds. Present → /chat dispatches the slow agent to
    # a background activation and polls, dodging the platform's ~40s
    # synchronous-HTTP limit; absent → /chat runs synchronously (local dev).
    do_fn_host: str | None
    do_fn_namespace: str | None
    do_fn_auth: str | None
    # Strava API app (https://www.strava.com/settings/api). All three present →
    # confirmed workouts are mirrored to Strava; absent → the feature is off.
    strava_client_id: str | None
    strava_client_secret: str | None
    strava_refresh_token: str | None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
            google_sa_json=os.getenv("GOOGLE_SA_JSON") or None,
            sheet_id=os.getenv("SHEET_ID") or None,
            passphrase=os.getenv("PASSPHRASE") or None,
            secret_key=os.getenv("SECRET_KEY") or None,
            model=os.getenv("MODEL") or DEFAULT_MODEL,
            fallback_model=os.getenv("FALLBACK_MODEL") or None,
            tool_mode=os.getenv("TOOL_MODE") or "native",
            # Local-dev switch: serve canned agent responses instead of calling
            # OpenRouter, so the UI and flows can be exercised off-quota.
            dry_run=_flag("DRY_RUN"),
            base_path=(os.getenv("BASE_PATH") or "").rstrip("/"),
            # The OpenWhisk runtime injects __OW_* on the deployed function only,
            # so async self-invoke turns itself on in production and stays off
            # locally with no config to manage. DO_FN_* override it for testing.
            do_fn_host=(os.getenv("DO_FN_HOST") or os.getenv("__OW_API_HOST") or "").rstrip("/")
            or None,
            do_fn_namespace=os.getenv("DO_FN_NAMESPACE") or os.getenv("__OW_NAMESPACE") or None,
            do_fn_auth=os.getenv("DO_FN_AUTH") or os.getenv("__OW_API_KEY") or None,
            strava_client_id=_opt("STRAVA_CLIENT_ID"),
            strava_client_secret=_opt("STRAVA_CLIENT_SECRET"),
            strava_refresh_token=_opt("STRAVA_REFRESH_TOKEN"),
        )

    @property
    def async_dispatch(self) -> bool:
        """True when self-invoke creds are configured (the deployed function)."""
        return bool(self.do_fn_host and self.do_fn_namespace and self.do_fn_auth)


settings = Settings.from_env()
