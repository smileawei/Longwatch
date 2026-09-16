from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


def load_dotenv(path: str | Path = ".env") -> None:
    """Load a small, predictable subset of .env syntax without another dependency."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def ensure_alert_link_secret(path: str | Path = ".env") -> str:
    """Return the persistent secret used to sign short-lived alert links."""
    environment_secret = os.getenv("ALERT_LINK_SECRET", "").strip()
    env_path = Path(path)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "ALERT_LINK_SECRET" and value.strip():
            secret = value.strip().strip("\"'")
            os.environ["ALERT_LINK_SECRET"] = secret
            return secret

    secret = environment_secret or secrets.token_urlsafe(32)
    output: list[str] = []
    replaced = False
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            if stripped.split("=", 1)[0].strip() == "ALERT_LINK_SECRET":
                output.append(f"ALERT_LINK_SECRET={secret}")
                replaced = True
                continue
        output.append(raw_line)
    if not replaced:
        if output and output[-1]:
            output.append("")
        output.append(f"ALERT_LINK_SECRET={secret}")
    os.environ["ALERT_LINK_SECRET"] = secret
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = env_path.with_suffix(f"{env_path.suffix}.tmp")
        temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
        os.replace(temporary, env_path)
    except OSError as exc:
        # Deployments that inject configuration through the environment can
        # mount .env read-only, and the container image leaves the working
        # directory unwritable. Failing here would abort startup even though
        # the secret is fully usable; the only cost of continuing is that a
        # restart issues a new one.
        logger.warning(
            "无法把新的告警链接密钥写入 %s，本次运行将继续使用内存中的密钥: %s",
            env_path,
            exc,
        )
    return secret


def _positive_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _positive_float_with_legacy(name: str, legacy_name: str, default: float) -> float:
    selected_name = name if os.getenv(name) is not None else legacy_name
    return _positive_float(selected_name, default)


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true 或 false")


@dataclass(frozen=True)
class Settings:
    oauth_client_id: str
    oauth_callback_port: int
    bark_device_key: str
    bark_server_url: str
    bark_group: str
    bark_level: str
    bark_sound: str
    poll_interval_seconds: float
    positions_refresh_seconds: float
    day_rise_pct: float
    day_fall_pct: float
    execution_rise_pct: float
    execution_fall_pct: float
    alert_hysteresis_pct: float
    include_extended_hours: bool
    quote_batch_size: int
    state_file: Path
    log_level: str
    http_host: str
    http_port: int
    alert_detail_base_url: str
    alert_link_secret: str

    @classmethod
    def from_env(cls) -> "Settings":
        http_port = _positive_int("HTTP_PORT", 8765)
        return cls(
            oauth_client_id=os.getenv("LONGBRIDGE_OAUTH_CLIENT_ID", "").strip(),
            oauth_callback_port=_positive_int("LONGBRIDGE_OAUTH_CALLBACK_PORT", 60355),
            bark_device_key=os.getenv("BARK_DEVICE_KEY", "").strip(),
            bark_server_url=os.getenv("BARK_SERVER_URL", "https://api.day.app").rstrip("/"),
            bark_group=os.getenv("BARK_GROUP", "LongWatch"),
            bark_level=os.getenv("BARK_LEVEL", "timeSensitive"),
            bark_sound=os.getenv("BARK_SOUND", ""),
            poll_interval_seconds=_positive_float("POLL_INTERVAL_SECONDS", 30),
            positions_refresh_seconds=_positive_float("POSITIONS_REFRESH_SECONDS", 300),
            day_rise_pct=_positive_float("DAY_RISE_PCT", 3),
            day_fall_pct=_positive_float("DAY_FALL_PCT", 3),
            execution_rise_pct=_positive_float_with_legacy(
                "EXECUTION_RISE_PCT", "COST_GAIN_PCT", 10
            ),
            execution_fall_pct=_positive_float_with_legacy(
                "EXECUTION_FALL_PCT", "COST_LOSS_PCT", 5
            ),
            alert_hysteresis_pct=_positive_float("ALERT_HYSTERESIS_PCT", 0.3),
            include_extended_hours=_bool("INCLUDE_EXTENDED_HOURS", False),
            quote_batch_size=_positive_int("QUOTE_BATCH_SIZE", 50),
            state_file=Path(os.getenv("STATE_FILE", "data/state.json")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            http_host=os.getenv("HTTP_HOST", "127.0.0.1"),
            http_port=http_port,
            alert_detail_base_url=os.getenv(
                "ALERT_DETAIL_BASE_URL", f"http://127.0.0.1:{http_port}"
            ).strip(),
            alert_link_secret=os.getenv("ALERT_LINK_SECRET", "").strip(),
        )

    def validate(self, require_bark: bool = True) -> None:
        self.validate_oauth()
        if require_bark and not self.bark_device_key:
            raise ValueError("缺少 BARK_DEVICE_KEY")
        if self.alert_detail_base_url:
            parsed = urlsplit(self.alert_detail_base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("ALERT_DETAIL_BASE_URL 必须是完整的 http 或 https 地址")

    def validate_oauth(self) -> None:
        if not self.oauth_client_id:
            raise ValueError("缺少 LONGBRIDGE_OAUTH_CLIENT_ID；LongWatch 仅支持 OAuth 认证")
