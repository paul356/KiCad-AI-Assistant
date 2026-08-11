"""SuperGrok (xAI) OAuth for the KiCad AI Assistant plugin.

Uses the same public OIDC client as the official Grok CLI:

  issuer:    https://auth.x.ai
  client_id: b1a00492-073a-47ea-816f-4c329264a828

Tokens are stored under the plugin's kcaa config dir.  If the user has already
logged in with ``grok login``, credentials in ``~/.grok/auth.json`` are reused
(and refreshed) automatically.

Inference goes through the SuperGrok CLI chat proxy:

  https://cli-chat-proxy.grok.com/v1/chat/completions

with the headers documented by xAI for auth.json / curl access.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

log = logging.getLogger(__name__)

# Official Grok CLI public OIDC client (PKCE / device-code, no secret).
OIDC_ISSUER = "https://auth.x.ai"
OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_SCOPES = "openid profile email offline_access grok-cli:access"

DEVICE_CODE_URL = f"{OIDC_ISSUER}/oauth2/device/code"
TOKEN_URL = f"{OIDC_ISSUER}/oauth2/token"
USERINFO_URL = f"{OIDC_ISSUER}/oauth2/userinfo"

# SuperGrok consumer proxy (OpenAI-compatible chat completions).
CLI_CHAT_PROXY_BASE = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_MODEL = "grok-4.5"
TOKEN_AUTH_HEADER_VALUE = "xai-grok-cli"

# Refresh a bit before actual expiry.
_REFRESH_SKEW_S = 120


@dataclass
class SuperGrokTokens:
    access_token: str
    refresh_token: str = ""
    expires_at: str = ""  # ISO-8601 UTC
    token_type: str = "Bearer"
    email: str = ""
    user_id: str = ""
    oidc_issuer: str = OIDC_ISSUER
    oidc_client_id: str = OIDC_CLIENT_ID

    def expired(self, skew_s: int = _REFRESH_SKEW_S) -> bool:
        if not self.expires_at:
            return True
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(timezone.utc) >= (exp - timedelta(seconds=skew_s))


def _auth_store_path(config_dir: str | None = None) -> Path:
    if config_dir:
        base = Path(config_dir)
    else:
        # Mirror settings.py default location.
        base = Path.home() / ".config" / "kicad" / "10.0" / "kcaa"
        # Prefer version detected from env if present.
        for key, val in os.environ.items():
            if key.startswith("KICAD") and key.endswith("_SYMBOL_DIR"):
                # e.g. KICAD10_SYMBOL_DIR → 10.0
                major = key[5:].split("_")[0]
                if major.isdigit():
                    base = Path.home() / ".config" / "kicad" / f"{major}.0" / "kcaa"
                    break
    base.mkdir(parents=True, exist_ok=True)
    return base / "supergrok_auth.json"


def _grok_cli_auth_path() -> Path:
    return Path.home() / ".grok" / "auth.json"


def _http_form(url: str, fields: dict[str, str], timeout: float = 30) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "kicad-ai-assistant-supergrok/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body)
        except json.JSONDecodeError:
            err = {"error": body[:300], "status": e.code}
        raise RuntimeError(
            f"OAuth HTTP {e.code}: {err.get('error_description') or err.get('error') or body[:200]}"
        ) from e


def _http_get_json(url: str, access_token: str, timeout: float = 20) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "kicad-ai-assistant-supergrok/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
        return json.loads(resp.read().decode("utf-8"))


def _expires_at_from_expires_in(expires_in: int | float | None) -> str:
    secs = int(expires_in or 3600)
    exp = datetime.now(timezone.utc) + timedelta(seconds=max(60, secs))
    return exp.isoformat().replace("+00:00", "Z")


def save_tokens(tokens: SuperGrokTokens, config_dir: str | None = None) -> Path:
    path = _auth_store_path(config_dir)
    payload = asdict(tokens)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    log.info("SuperGrok tokens saved to %s (email=%s)", path, tokens.email or "?")
    return path


def load_tokens(config_dir: str | None = None) -> SuperGrokTokens | None:
    """Load plugin-stored tokens, falling back to ~/.grok/auth.json."""
    path = _auth_store_path(config_dir)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tok = SuperGrokTokens(
                access_token=data.get("access_token") or data.get("key") or "",
                refresh_token=data.get("refresh_token") or "",
                expires_at=data.get("expires_at") or "",
                token_type=data.get("token_type") or "Bearer",
                email=data.get("email") or "",
                user_id=data.get("user_id") or data.get("principal_id") or "",
                oidc_issuer=data.get("oidc_issuer") or OIDC_ISSUER,
                oidc_client_id=data.get("oidc_client_id") or OIDC_CLIENT_ID,
            )
            if tok.access_token:
                return tok
        except (OSError, json.JSONDecodeError, TypeError) as e:
            log.warning("Could not read SuperGrok auth store: %s", e)

    return _load_from_grok_cli_auth()


def _load_from_grok_cli_auth() -> SuperGrokTokens | None:
    path = _grok_cli_auth_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.debug("Could not read ~/.grok/auth.json: %s", e)
        return None
    if not isinstance(data, dict):
        return None

    # Prefer entries for auth.x.ai / accounts.x.ai.
    preferred: list[tuple[str, dict]] = []
    others: list[tuple[str, dict]] = []
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        bucket = preferred if ("auth.x.ai" in key or "accounts.x.ai" in key) else others
        bucket.append((key, entry))
    for _key, entry in preferred + others:
        access = entry.get("key") or entry.get("access_token") or ""
        if not access:
            continue
        return SuperGrokTokens(
            access_token=access,
            refresh_token=entry.get("refresh_token") or "",
            expires_at=entry.get("expires_at") or "",
            token_type="Bearer",
            email=entry.get("email") or "",
            user_id=entry.get("user_id") or entry.get("principal_id") or "",
            oidc_issuer=entry.get("oidc_issuer") or OIDC_ISSUER,
            oidc_client_id=entry.get("oidc_client_id") or OIDC_CLIENT_ID,
        )
    return None


def clear_tokens(config_dir: str | None = None) -> None:
    path = _auth_store_path(config_dir)
    try:
        if path.is_file():
            path.unlink()
            log.info("Cleared SuperGrok auth store %s", path)
    except OSError as e:
        log.warning("Could not clear SuperGrok auth: %s", e)


def refresh_tokens(tokens: SuperGrokTokens, config_dir: str | None = None) -> SuperGrokTokens:
    if not tokens.refresh_token:
        raise RuntimeError("No refresh_token available — please log in again with SuperGrok.")
    body = _http_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": tokens.oidc_client_id or OIDC_CLIENT_ID,
        },
    )
    access = body.get("access_token") or ""
    if not access:
        raise RuntimeError(f"Token refresh returned no access_token: {body}")
    new = SuperGrokTokens(
        access_token=access,
        refresh_token=body.get("refresh_token") or tokens.refresh_token,
        expires_at=_expires_at_from_expires_in(body.get("expires_in")),
        token_type=body.get("token_type") or "Bearer",
        email=tokens.email,
        user_id=tokens.user_id,
        oidc_issuer=tokens.oidc_issuer or OIDC_ISSUER,
        oidc_client_id=tokens.oidc_client_id or OIDC_CLIENT_ID,
    )
    # Best-effort profile fill.
    if not new.email:
        try:
            info = _http_get_json(USERINFO_URL, new.access_token)
            new.email = info.get("email") or ""
            new.user_id = info.get("sub") or new.user_id
        except Exception as e:
            log.debug("userinfo after refresh failed: %s", e)
    save_tokens(new, config_dir)
    return new


def get_valid_access_token(config_dir: str | None = None) -> str:
    """Return a usable Bearer access token, refreshing if needed.

    Raises RuntimeError if the user must log in.
    """
    tokens = load_tokens(config_dir)
    if tokens is None:
        raise RuntimeError(
            "Not logged in to SuperGrok. Open Settings → Login with SuperGrok "
            "(or run `grok login` in a terminal)."
        )
    if tokens.expired():
        log.info("SuperGrok access token expired/near-expiry — refreshing")
        tokens = refresh_tokens(tokens, config_dir)
    return tokens.access_token


def proxy_headers(access_token: str, model: str) -> dict[str, str]:
    """Headers required by cli-chat-proxy.grok.com for SuperGrok session tokens."""
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-XAI-Token-Auth": TOKEN_AUTH_HEADER_VALUE,
        "x-grok-model-override": model or DEFAULT_MODEL,
        "Accept": "text/event-stream",
        "User-Agent": "kicad-ai-assistant-supergrok/1.0",
    }


def proxy_chat_url(base_url: str | None = None) -> str:
    base = (base_url or CLI_CHAT_PROXY_BASE).rstrip("/")
    if "/chat/completions" in base:
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


# ---------------------------------------------------------------------------
# Device-code login (interactive)
# ---------------------------------------------------------------------------


@dataclass
class DeviceCodeSession:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


def start_device_login() -> DeviceCodeSession:
    """Begin OAuth device-code login. Caller should show user_code to the user."""
    body = _http_form(
        DEVICE_CODE_URL,
        {
            "client_id": OIDC_CLIENT_ID,
            "scope": OIDC_SCOPES,
        },
    )
    device_code = body.get("device_code") or ""
    user_code = body.get("user_code") or ""
    if not device_code or not user_code:
        raise RuntimeError(f"Device code response incomplete: {body}")
    uri = body.get("verification_uri") or "https://auth.x.ai/activate"
    uri_complete = body.get("verification_uri_complete") or uri
    return DeviceCodeSession(
        device_code=device_code,
        user_code=user_code,
        verification_uri=uri,
        verification_uri_complete=uri_complete,
        expires_in=int(body.get("expires_in") or 600),
        interval=max(2, int(body.get("interval") or 5)),
    )


def poll_device_login(
    session: DeviceCodeSession,
    *,
    config_dir: str | None = None,
    cancel_check=None,
    open_browser: bool = True,
) -> SuperGrokTokens:
    """Poll until the user approves the device code (or timeout / cancel)."""
    if open_browser:
        try:
            webbrowser.open(session.verification_uri_complete or session.verification_uri)
        except Exception as e:
            log.debug("Could not open browser: %s", e)

    deadline = time.monotonic() + max(60, session.expires_in)
    interval = session.interval
    while time.monotonic() < deadline:
        if cancel_check is not None and cancel_check():
            raise RuntimeError("SuperGrok login cancelled")
        try:
            body = _http_form(
                TOKEN_URL,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": session.device_code,
                    "client_id": OIDC_CLIENT_ID,
                },
            )
        except RuntimeError as e:
            msg = str(e).lower()
            # Standard device-flow pending errors.
            if "authorization_pending" in msg or "slow_down" in msg:
                if "slow_down" in msg:
                    interval = min(interval + 2, 30)
                time.sleep(interval)
                continue
            if "expired_token" in msg or "access_denied" in msg:
                raise
            # Some servers wrap pending in generic 400 text — keep polling a bit.
            if "400" in msg and "invalid_grant" not in msg:
                time.sleep(interval)
                continue
            raise

        access = body.get("access_token") or ""
        if not access:
            time.sleep(interval)
            continue

        tokens = SuperGrokTokens(
            access_token=access,
            refresh_token=body.get("refresh_token") or "",
            expires_at=_expires_at_from_expires_in(body.get("expires_in")),
            token_type=body.get("token_type") or "Bearer",
        )
        try:
            info = _http_get_json(USERINFO_URL, tokens.access_token)
            tokens.email = info.get("email") or ""
            tokens.user_id = info.get("sub") or ""
        except Exception as e:
            log.debug("userinfo after device login failed: %s", e)
        save_tokens(tokens, config_dir)
        return tokens

    raise RuntimeError("SuperGrok login timed out — please try again.")


def status_summary(config_dir: str | None = None) -> str:
    tok = load_tokens(config_dir)
    if tok is None:
        return "Not logged in"
    who = tok.email or tok.user_id or "SuperGrok user"
    if tok.expired(skew_s=0):
        return f"Logged in as {who} (token expired — will refresh on use)"
    return f"Logged in as {who}"
