from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SENSITIVE_KEYS = {
    "cpf",
    "cpf_esperado",
    "email",
    "telefone",
    "password",
    "senha",
    "certificate",
    "certificado",
    "logradouro",
    "numero",
    "complemento",
    "cep",
}


class AuditLog:
    def __init__(self, root: Path, run_id: str | None = None) -> None:
        self.run_id = run_id or str(uuid.uuid4())
        self.run_dir = root / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.path = self.run_dir / "audit.jsonl"
        self._previous_hash = "GENESIS"
        self._sequence = 0
        self.on_record = None  # callback opcional(event, state) para a pagina local

    def record(self, event: str, state: str, details: dict[str, Any] | None = None) -> None:
        self._sequence += 1
        payload = {
            "run_id": self.run_id,
            "sequence": self._sequence,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "state": state,
            "details": _redact(details or {}),
            "previous_hash": self._previous_hash,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        current_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        payload["hash"] = current_hash
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._previous_hash = current_hash
        if self.on_record:
            self.on_record(event, state)

    def write_result(self, result: dict[str, Any]) -> Path:
        path = self.run_dir / "result.json"
        safe = _redact(result)
        path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def verify_audit_file(path: Path) -> tuple[bool, str]:
    previous = "GENESIS"
    expected_sequence = 1
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        payload = json.loads(line)
        current_hash = payload.pop("hash", None)
        if payload.get("sequence") != expected_sequence:
            return False, f"Sequencia invalida na linha {line_no}"
        if payload.get("previous_hash") != previous:
            return False, f"Encadeamento invalido na linha {line_no}"
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        calculated = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if current_hash != calculated:
            return False, f"Hash invalido na linha {line_no}"
        previous = current_hash
        expected_sequence += 1
    return True, "OK"


def _redact(value: Any, key: str = "") -> Any:
    if key.lower() in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    return value
