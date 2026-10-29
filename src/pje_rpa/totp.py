"""Codigo do segundo fator (TOTP, RFC 6238) gerado a partir da chave secreta do advogado.

E o mesmo calculo do app autenticador (Google Authenticator, Microsoft Authenticator etc.):
a chave e a que aparece no QR code ao configurar o 2FA no PDPJ. Aceita a chave em base32
(com ou sem espacos) ou a URI otpauth:// inteira. A chave nunca e gravada nem registrada.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import struct
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

from .config import ConfigurationError

_ALGORITHMS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}


@dataclass(frozen=True)
class TotpKey:
    secret: bytes = field(repr=False)
    digits: int = 6
    period: int = 30
    algorithm: str = "SHA1"

    def code(self, now: float | None = None) -> str:
        counter = int((time.time() if now is None else now) // self.period)
        digest = hmac.new(self.secret, struct.pack(">Q", counter), _ALGORITHMS[self.algorithm]).digest()
        offset = digest[-1] & 0x0F
        value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        return str(value % 10 ** self.digits).zfill(self.digits)

    def seconds_left(self, now: float | None = None) -> float:
        return self.period - (time.time() if now is None else now) % self.period

    def fresh_code(self, min_seconds: float = 5) -> str:
        """Codigo com folga de validade: perto da virada, espera o proximo para nao expirar no envio."""
        left = self.seconds_left()
        if left < min_seconds:
            time.sleep(left + 0.5)
        return self.code()


def parse_totp(value: str) -> TotpKey:
    text = value.strip()
    if re.fullmatch(r"\d{6,8}", text):
        raise ConfigurationError("2FA: a planilha tem um codigo de 6 digitos, que expira em 30 s; "
                                 "informe a chave secreta do autenticador")
    digits, period, algorithm = 6, 30, "SHA1"
    if text.lower().startswith("otpauth://"):
        query = parse_qs(urlparse(text).query)
        text = unquote(query.get("secret", [""])[0])
        digits = int(query.get("digits", ["6"])[0])
        period = int(query.get("period", ["30"])[0])
        algorithm = query.get("algorithm", ["SHA1"])[0].upper()
        if algorithm not in _ALGORITHMS or not 6 <= digits <= 8 or period <= 0:
            raise ConfigurationError("2FA: parametros da URI otpauth nao suportados")
    cleaned = re.sub(r"[\s-]", "", text).upper().rstrip("=")
    if not re.fullmatch(r"[A-Z2-7]{16,}", cleaned):
        raise ConfigurationError("2FA: chave secreta invalida (esperado texto base32, ex.: JBSW Y3DP EHPK 3PXP)")
    try:
        secret = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except binascii.Error as exc:
        raise ConfigurationError("2FA: chave secreta invalida") from exc
    return TotpKey(secret, digits, period, algorithm)
