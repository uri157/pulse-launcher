from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_KDF_ITERATIONS = 390000
_SUPPORTED_PROVIDERS = {
    "local_encrypted_file",
    "local_encrypted",
    "encrypted_file",
}
_SELECTOR_FIELDS = (
    "venue",
    "strategy_fingerprint",
    "deployment_id",
)


class KeyringError(RuntimeError):
    pass


@dataclass(frozen=True)
class CredentialSelector:
    venue: str = ""
    strategy_fingerprint: str = ""
    deployment_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "venue": self.venue,
            "strategy_fingerprint": self.strategy_fingerprint,
            "deployment_id": self.deployment_id,
        }


@dataclass(frozen=True)
class CredentialRecord:
    selector: CredentialSelector
    api_key: str
    api_secret: str
    created_at: str
    updated_at: str
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "selector": self.selector.as_dict(),
            "api_key": self.api_key,
            "api_secret": self.api_secret,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.label:
            payload["label"] = self.label
        return payload


def supported_provider(provider: str) -> bool:
    return provider.strip().lower() in _SUPPORTED_PROVIDERS


def normalize_selector(payload: dict[str, Any]) -> CredentialSelector:
    if not isinstance(payload, dict):
        raise KeyringError("credentials.selector must be an object")

    normalized: dict[str, str] = {}
    for field_name in _SELECTOR_FIELDS:
        value = payload.get(field_name, "")
        if value is None:
            value = ""
        if not isinstance(value, (str, int, float)):
            raise KeyringError(f"credentials.selector.{field_name} must be a string")
        normalized[field_name] = str(value).strip()

    normalized["venue"] = normalized["venue"].lower()
    normalized["strategy_fingerprint"] = normalized["strategy_fingerprint"].lower()

    return CredentialSelector(**normalized)


def selector_missing_required(selector: CredentialSelector) -> list[str]:
    required = (
        "venue",
        "strategy_fingerprint",
    )
    missing: list[str] = []
    for field_name in required:
        if not getattr(selector, field_name):
            missing.append(field_name)
    return missing


def build_strategy_fingerprint(
    *,
    strategy_name: str,
    strategy_payload: dict[str, Any],
) -> str:
    normalized_name = str(strategy_name or "").strip().lower()
    if not normalized_name:
        raise KeyringError("strategy_name is required to derive credentials selector")
    canonical_payload = {
        "strategy_name": normalized_name,
        "strategy": strategy_payload if isinstance(strategy_payload, dict) else {},
    }
    canonical = json.dumps(
        canonical_payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_encrypted_keyring(path: Path, passphrase: str) -> list[CredentialRecord]:
    if not path.exists():
        return []

    try:
        raw = path.read_text(encoding="utf-8")
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KeyringError(f"invalid keyring JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise KeyringError(f"cannot read keyring file {path}: {exc}") from exc

    if not isinstance(envelope, dict):
        raise KeyringError(f"keyring root must be an object: {path}")

    version = int(envelope.get("version", 1))
    if version != 1:
        raise KeyringError(f"unsupported keyring version {version} in {path}")

    kdf_payload = envelope.get("kdf")
    token = envelope.get("token")
    if not isinstance(kdf_payload, dict) or not isinstance(token, str):
        raise KeyringError(f"invalid keyring envelope in {path}")

    salt_b64 = str(kdf_payload.get("salt_b64") or "").strip()
    iterations = int(kdf_payload.get("iterations", _KDF_ITERATIONS))
    if not salt_b64:
        raise KeyringError(f"invalid keyring envelope (missing kdf salt) in {path}")

    fernet = _build_fernet(
        passphrase=passphrase,
        salt_b64=salt_b64,
        iterations=iterations,
    )
    try:
        plaintext = fernet.decrypt(token.encode("utf-8"))
    except Exception as exc:
        raise KeyringError(
            "could not decrypt keyring. Verify passphrase and keyring file."
        ) from exc

    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise KeyringError(f"decrypted keyring payload is invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise KeyringError("decrypted keyring payload must be an object")

    entries = payload.get("entries", [])
    if not isinstance(entries, list):
        raise KeyringError("decrypted keyring payload.entries must be an array")

    out: list[CredentialRecord] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise KeyringError(f"keyring entry {idx} must be an object")
        selector = normalize_selector(dict(entry.get("selector") or {}))
        api_key = str(entry.get("api_key") or "").strip()
        api_secret = str(entry.get("api_secret") or "").strip()
        if not api_key or not api_secret:
            raise KeyringError(f"keyring entry {idx} is missing api_key/api_secret")
        created_at = str(entry.get("created_at") or "")
        updated_at = str(entry.get("updated_at") or "")
        label = str(entry.get("label") or "")
        out.append(
            CredentialRecord(
                selector=selector,
                api_key=api_key,
                api_secret=api_secret,
                created_at=created_at,
                updated_at=updated_at,
                label=label,
            )
        )
    return out


def write_encrypted_keyring(
    path: Path,
    passphrase: str,
    records: list[CredentialRecord],
) -> None:
    now_iso = _utc_now_iso()
    normalized_records = sorted(records, key=lambda item: item.updated_at or now_iso)
    payload = {
        "version": 1,
        "entries": [record.as_dict() for record in normalized_records],
    }
    plaintext = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

    salt = os.urandom(16)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    fernet = _build_fernet(
        passphrase=passphrase,
        salt_b64=salt_b64,
        iterations=_KDF_ITERATIONS,
    )
    token = fernet.encrypt(plaintext).decode("utf-8")

    envelope = {
        "version": 1,
        "cipher": "fernet",
        "kdf": {
            "name": "pbkdf2_sha256",
            "iterations": _KDF_ITERATIONS,
            "salt_b64": salt_b64,
        },
        "token": token,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, indent=2, ensure_ascii=True), encoding="utf-8")


def find_by_selector(
    records: list[CredentialRecord],
    selector: CredentialSelector,
) -> CredentialRecord | None:
    matches: list[CredentialRecord] = []
    for record in records:
        if _selector_matches(record.selector, selector):
            matches.append(record)

    if not matches:
        return None

    if len(matches) > 1:
        raise KeyringError(
            "multiple credentials matched selector. Add 'deployment_id' (or narrow selector)."
        )

    return matches[0]


def upsert_record(
    records: list[CredentialRecord],
    *,
    selector: CredentialSelector,
    api_key: str,
    api_secret: str,
    label: str = "",
) -> list[CredentialRecord]:
    now_iso = _utc_now_iso()
    selector_key = _selector_key(selector)
    next_records: list[CredentialRecord] = []
    replaced = False

    for record in records:
        if _selector_key(record.selector) == selector_key:
            next_records.append(
                CredentialRecord(
                    selector=selector,
                    api_key=api_key,
                    api_secret=api_secret,
                    created_at=record.created_at or now_iso,
                    updated_at=now_iso,
                    label=label or record.label,
                )
            )
            replaced = True
        else:
            next_records.append(record)

    if not replaced:
        next_records.append(
            CredentialRecord(
                selector=selector,
                api_key=api_key,
                api_secret=api_secret,
                created_at=now_iso,
                updated_at=now_iso,
                label=label,
            )
        )

    return next_records


def _selector_key(selector: CredentialSelector) -> tuple[str, ...]:
    return tuple(getattr(selector, field_name) for field_name in _SELECTOR_FIELDS)


def _selector_matches(candidate: CredentialSelector, query: CredentialSelector) -> bool:
    for field_name in _SELECTOR_FIELDS:
        query_value = getattr(query, field_name)
        if not query_value:
            continue
        if getattr(candidate, field_name) != query_value:
            return False
    return True


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_fernet(*, passphrase: str, salt_b64: str, iterations: int):
    if not passphrase:
        raise KeyringError("keyring passphrase cannot be empty")
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError as exc:
        raise KeyringError(
            "missing 'cryptography' dependency. Install pulse-launcher requirements."
        ) from exc

    try:
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
    except Exception as exc:
        raise KeyringError("invalid keyring kdf salt") from exc

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    return Fernet(key)
