"""Settings: credentials, cached session location and per-user invoice defaults.

Lookup order for every value: environment variable first, then the TOML config file
(~/.config/fiscozen/config.toml). The password is never stored in the config file:
it comes from FISCOZEN_PASSWORD or the macOS Keychain.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("FISCOZEN_CONFIG_DIR", Path.home() / ".config" / "fiscozen"))
CONFIG_FILE = CONFIG_DIR / "config.toml"
SESSION_FILE = CONFIG_DIR / "session.json"

# Keychain item: security add-generic-password -s fiscozen-mcp -a <email> -w
KEYCHAIN_SERVICE = "fiscozen-mcp"


class ConfigError(RuntimeError):
    """Missing or invalid local configuration."""


@dataclass(frozen=True)
class Settings:
    email: str
    # Optional defaults; when missing they are resolved from the account at call time
    default_client_id: int | None = None
    default_payment_method_id: int | None = None
    ateco: str | None = None
    session_file: Path = SESSION_FILE

    @classmethod
    def load(cls) -> Settings:
        file_values: dict = {}
        if CONFIG_FILE.exists():
            file_values = tomllib.loads(CONFIG_FILE.read_text())

        email = os.environ.get("FISCOZEN_EMAIL") or file_values.get("email")
        if not email:
            raise ConfigError(f"Set FISCOZEN_EMAIL or `email` in {CONFIG_FILE}")

        def optional_int(env_name: str, key: str) -> int | None:
            raw = os.environ.get(env_name) or file_values.get(key)
            return int(raw) if raw not in (None, "") else None

        return cls(
            email=email,
            default_client_id=optional_int("FISCOZEN_DEFAULT_CLIENT_ID", "default_client_id"),
            default_payment_method_id=optional_int(
                "FISCOZEN_DEFAULT_PAYMENT_METHOD_ID", "default_payment_method_id"
            ),
            ateco=os.environ.get("FISCOZEN_ATECO") or file_values.get("ateco"),
        )

    def password(self) -> str:
        """Password from env, else from the macOS Keychain. Never cached on disk."""
        if password := os.environ.get("FISCOZEN_PASSWORD"):
            return password
        try:
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-a",
                    self.email,
                    "-w",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise ConfigError(
                "No password: set FISCOZEN_PASSWORD or run "
                f"`security add-generic-password -s {KEYCHAIN_SERVICE} -a {self.email} -w`"
            ) from exc
        return result.stdout.rstrip("\n")
