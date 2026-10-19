from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from .models import Address, AppConfig, BrowserConfig, LawyerRecord, digits
from .signer import SignerConfig


class ConfigurationError(ValueError):
    pass


def load_config(path: Path) -> AppConfig:
    with path.open("rb") as stream:
        raw = tomllib.load(stream)

    app = raw.get("app", {})
    browser = raw.get("browser", {})
    base_url = str(app.get("base_url", "")).strip()
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigurationError("app.base_url deve ser uma URL HTTPS valida")

    selectors = _string_lists(raw.get("selectors", {}), "selectors")
    stop_markers = _string_lists(raw.get("stop_markers", {}), "stop_markers")
    required = {
        "login_certificate_button",
        "registration_form",
        "detected_name",
        "detected_cpf",
        "detected_oab",
        "email",
        "phone",
        "cep",
        "logradouro",
        "address_number",
        "bairro",
        "cidade",
        "uf",
        "confirm_data",
        "commitment_term",
        "sign_term",
        "completion",
        "lawyer_panel",
    }
    missing = sorted(required - selectors.keys())
    if missing:
        raise ConfigurationError(f"Seletores obrigatorios ausentes: {', '.join(missing)}")

    return AppConfig(
        environment=str(app.get("environment", "simulation")),
        tribunal=str(app.get("tribunal", "NAO_CONFIGURADO")),
        base_url=base_url,
        selectors_approved=bool(app.get("selectors_approved", False)),
        allow_live_submission=bool(app.get("allow_live_submission", False)),
        browser=BrowserConfig(
            engine=str(browser.get("engine", "chromium")),
            channel=str(browser.get("channel", "msedge")),
            headless=bool(browser.get("headless", False)),
            timeout_ms=int(browser.get("timeout_ms", 30000)),
        ),
        selectors=selectors,
        stop_markers=stop_markers,
        signer=_signer_config(raw.get("signer", {})),
        require_authorization_ref=bool(app.get("require_authorization_ref", True)),
    )


def _signer_config(raw: dict) -> SignerConfig:
    defaults = SignerConfig()
    return SignerConfig(
        window_title_regex=str(raw.get("window_title_regex", defaults.window_title_regex)),
        appear_timeout_s=int(raw.get("appear_timeout_s", defaults.appear_timeout_s)),
        max_dialogs=int(raw.get("max_dialogs", defaults.max_dialogs)),
        jab_dll=str(raw.get("jab_dll", defaults.jab_dll)),
        store_option=str(raw.get("store_option", defaults.store_option)),
        ok_button=str(raw.get("ok_button", defaults.ok_button)),
    )


def load_lawyer(path: Path) -> LawyerRecord:
    return lawyer_from_dict(json.loads(path.read_text(encoding="utf-8")))


def lawyer_from_dict(raw: dict) -> LawyerRecord:
    address = raw.get("endereco", {})
    record = LawyerRecord(
        nome_esperado=str(raw.get("nome_esperado", "")).strip(),
        cpf_esperado=digits(str(raw.get("cpf_esperado", ""))),
        oab_numero=digits(str(raw.get("oab_numero", ""))),
        oab_uf=str(raw.get("oab_uf", "")).strip().upper(),
        email=str(raw.get("email", "")).strip(),
        telefone=digits(str(raw.get("telefone", ""))),
        endereco=Address(
            cep=digits(str(address.get("cep", ""))),
            logradouro=str(address.get("logradouro", "")).strip(),
            numero=str(address.get("numero", "")).strip(),
            complemento=str(address.get("complemento", "")).strip(),
            bairro=str(address.get("bairro", "")).strip(),
            cidade=str(address.get("cidade", "")).strip(),
            uf=str(address.get("uf", "")).strip().upper(),
        ),
        autorizacao_ref=str(raw.get("autorizacao_ref", "")).strip(),
    )
    errors = validate_lawyer(record)
    if errors:
        raise ConfigurationError("; ".join(errors))
    return record


def validate_lawyer(record: LawyerRecord) -> list[str]:
    errors: list[str] = []
    if len(record.nome_esperado) < 5:
        errors.append("nome_esperado invalido")
    if len(record.cpf_esperado) != 11 or not _valid_cpf(record.cpf_esperado):
        errors.append("cpf_esperado invalido")
    if not record.oab_numero:
        errors.append("oab_numero obrigatorio")
    if not re.fullmatch(r"[A-Z]{2}", record.oab_uf):
        errors.append("oab_uf invalida")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", record.email):
        errors.append("email invalido")
    if not 10 <= len(record.telefone) <= 11:
        errors.append("telefone invalido")
    if len(record.endereco.cep) != 8:
        errors.append("CEP invalido")
    for name in ("logradouro", "numero", "bairro", "cidade"):
        if not getattr(record.endereco, name):
            errors.append(f"endereco.{name} obrigatorio")
    if not re.fullmatch(r"[A-Z]{2}", record.endereco.uf):
        errors.append("endereco.uf invalida")
    return errors


def assert_live_ready(config: AppConfig, record: LawyerRecord | None = None) -> None:
    if config.environment != "production":
        raise ConfigurationError("Modo live exige app.environment = 'production'")
    if not config.selectors_approved:
        raise ConfigurationError("Seletores ainda nao foram homologados")
    if not config.allow_live_submission:
        raise ConfigurationError("Envio real permanece bloqueado por configuracao")
    _assert_official_target(config, record)


def assert_rehearsal_ready(config: AppConfig, record: LawyerRecord | None = None) -> None:
    """Ensaio usa o PJe real, mas nao envia nada: dispensa a homologacao dos seletores e a
    chave de envio, que e justamente o que o ensaio serve para validar."""
    if config.environment != "production":
        raise ConfigurationError("Ensaio exige app.environment = 'production'")
    _assert_official_target(config, record)


def _assert_official_target(config: AppConfig, record: LawyerRecord | None) -> None:
    if "exemplo" in config.base_url.lower() or "example" in config.base_url.lower():
        raise ConfigurationError("Substitua a URL de exemplo pela URL oficial do tribunal")
    if record and config.require_authorization_ref and len(record.autorizacao_ref) < 3:
        raise ConfigurationError("Informe a referencia da autorizacao do advogado (autorizacao_ref)")
    if record and ("TESTE" in record.nome_esperado.upper() or record.email.endswith("@example.com")):
        raise ConfigurationError("Substitua os dados de exemplo antes do modo live")
    hostname = (urlparse(config.base_url).hostname or "").lower()
    if not hostname.endswith(".jus.br"):
        raise ConfigurationError("Modo live aceita somente dominio oficial terminado em .jus.br")


def _string_lists(value: object, label: str) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"Secao [{label}] invalida")
    result: dict[str, list[str]] = {}
    for key, items in value.items():
        if not isinstance(items, list) or not items or not all(isinstance(x, str) and x for x in items):
            raise ConfigurationError(f"{label}.{key} deve ser uma lista nao vazia de textos")
        result[str(key)] = list(items)
    return result


def _valid_cpf(number: str) -> bool:
    if len(number) != 11 or number == number[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(number[i]) * (size + 1 - i) for i in range(size))
        digit = (total * 10 % 11) % 10
        if digit != int(number[size]):
            return False
    return True
