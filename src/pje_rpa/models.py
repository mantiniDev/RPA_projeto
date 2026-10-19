from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .signer import SignerConfig


class FlowState(str, Enum):
    START = "START"
    LOGIN = "LOGIN"
    WAITING_CERTIFICATE = "WAITING_CERTIFICATE"
    SECOND_FACTOR = "SECOND_FACTOR"
    REGISTRATION_FORM = "REGISTRATION_FORM"
    REVIEW = "REVIEW"
    COMMITMENT_TERM = "COMMITMENT_TERM"
    COMPLETED = "COMPLETED"
    ALREADY_REGISTERED = "ALREADY_REGISTERED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    COMPLETED = "COMPLETED"
    ALREADY_REGISTERED = "ALREADY_REGISTERED"
    REHEARSAL_OK = "REHEARSAL_OK"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Address:
    cep: str
    logradouro: str
    numero: str
    complemento: str
    bairro: str
    cidade: str
    uf: str


@dataclass(frozen=True)
class LawyerRecord:
    nome_esperado: str
    cpf_esperado: str
    oab_numero: str
    oab_uf: str
    email: str
    telefone: str
    endereco: Address
    autorizacao_ref: str = ""

    def safe_summary(self) -> dict[str, Any]:
        return {
            "nome": mask_name(self.nome_esperado),
            "cpf": mask_cpf(self.cpf_esperado),
            "oab": f"{self.oab_numero}/{self.oab_uf}",
            "email": mask_email(self.email),
            "telefone": mask_phone(self.telefone),
            "cidade_uf": f"{self.endereco.cidade}/{self.endereco.uf}",
            "autorizacao_ref": self.autorizacao_ref,
        }


@dataclass(frozen=True)
class BrowserConfig:
    engine: str
    channel: str
    headless: bool
    timeout_ms: int


@dataclass(frozen=True)
class AppConfig:
    environment: str
    tribunal: str
    base_url: str
    selectors_approved: bool
    allow_live_submission: bool
    browser: BrowserConfig
    selectors: dict[str, list[str]]
    stop_markers: dict[str, list[str]]
    signer: "SignerConfig | None" = None
    require_authorization_ref: bool = True


@dataclass
class RunResult:
    run_id: str
    status: RunStatus
    state: FlowState
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


def digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def mask_cpf(value: str) -> str:
    number = digits(value)
    return f"***.***.***-{number[-2:]}" if len(number) == 11 else "***"


def mask_email(value: str) -> str:
    local, sep, domain = value.partition("@")
    if not sep:
        return "***"
    shown = local[:1] if local else ""
    return f"{shown}***@{domain}"


def mask_phone(value: str) -> str:
    number = digits(value)
    return f"(**) *****-{number[-4:]}" if len(number) >= 4 else "***"


def mask_name(value: str) -> str:
    parts = value.split()
    return " ".join(f"{part[:1]}***" for part in parts) if parts else "***"
