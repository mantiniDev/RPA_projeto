"""Leitura do certificado A1 e instalacao temporaria no repositorio do Windows.

O arquivo .pfx e a senha ficam apenas em memoria. Para que o PJeOffice PRO oficial
encontre o certificado, ele e importado no repositorio "Pessoal" do usuario Windows
como chave NAO exportavel e removido (com a chave privada) ao final da execucao.
"""
from __future__ import annotations

import base64
import platform
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

# ICP-Brasil, certificado de pessoa fisica: OID 2.16.76.1.3.1
# Conteudo: data de nascimento (DDMMAAAA) + CPF (11) + NIS (11) + RG + orgao/UF
ICP_BRASIL_PF_OID = "2.16.76.1.3.1"


class CertificateError(ValueError):
    pass


@dataclass(frozen=True)
class CertificateInfo:
    nome: str
    cpf: str
    emissor: str
    valido_ate: datetime
    thumbprint_sha1: str

    def safe_summary(self) -> dict[str, str]:
        from .models import mask_cpf, mask_name

        return {
            "titular": mask_name(self.nome),
            "cpf_certificado": mask_cpf(self.cpf),
            "emissor": self.emissor,
            "valido_ate": self.valido_ate.date().isoformat(),
            "thumbprint": self.thumbprint_sha1,
        }


def read_pfx(data: bytes, password: str) -> CertificateInfo:
    try:
        key, cert, _chain = pkcs12.load_key_and_certificates(data, password.encode("utf-8"))
    except ValueError as exc:
        raise CertificateError("Senha incorreta ou arquivo nao e um certificado A1 (.pfx/.p12)") from exc
    if cert is None or key is None:
        raise CertificateError("O arquivo nao contem certificado e chave privada")

    now = datetime.now(timezone.utc)
    not_after = cert.not_valid_after_utc
    if not_after < now:
        raise CertificateError(f"Certificado expirado em {not_after.date().isoformat()}")
    if cert.not_valid_before_utc > now:
        raise CertificateError("Certificado ainda nao esta valido")

    cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not cn_attrs:
        raise CertificateError("Certificado sem nome do titular (CN)")
    common_name = str(cn_attrs[0].value)
    nome = common_name.split(":")[0].strip()

    cpf = _cpf_from_san(cert) or _cpf_from_cn(common_name)
    if not cpf:
        raise CertificateError("CPF do titular nao encontrado; use um certificado ICP-Brasil de pessoa fisica")

    issuer_cn = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    from cryptography.hazmat.primitives import hashes

    return CertificateInfo(
        nome=nome,
        cpf=cpf,
        emissor=str(issuer_cn[0].value) if issuer_cn else "",
        valido_ate=not_after,
        thumbprint_sha1=cert.fingerprint(hashes.SHA1()).hex().upper(),
    )


def _cpf_from_san(cert: x509.Certificate) -> str | None:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    for other in san.get_values_for_type(x509.OtherName):
        if other.type_id.dotted_string != ICP_BRASIL_PF_OID:
            continue
        text = _der_string_payload(other.value)
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 19:
            return digits[8:19]
    return None


def _cpf_from_cn(common_name: str) -> str | None:
    # Formato usual: "NOME DO TITULAR:12345678901"
    _, sep, tail = common_name.rpartition(":")
    digits = "".join(ch for ch in tail if ch.isdigit())
    return digits if sep and len(digits) == 11 else None


def _der_string_payload(raw: bytes) -> str:
    """Extrai o conteudo de um OCTET/PRINTABLE/UTF8 STRING DER (com ou sem tag [0] explicita)."""
    data = raw
    for _ in range(3):
        if len(data) < 2:
            break
        tag = data[0]
        length = data[1]
        offset = 2
        if length & 0x80:
            count = length & 0x7F
            length = int.from_bytes(data[2 : 2 + count], "big")
            offset = 2 + count
        body = data[offset : offset + length]
        if tag in (0x04, 0x0C, 0x13, 0x16):
            return body.decode("latin-1", errors="ignore")
        data = body
    return data.decode("latin-1", errors="ignore")


# --------------------------------------------------------------------------- Windows


def _rewrap_for_legacy_csp(data: bytes, password: str) -> tuple[bytes, str]:
    """Reempacota o .pfx, em memoria, sem o atributo que escolhe o provedor de chaves.

    Muitos .pfx de AC trazem o atributo "Microsoft CSP Name" apontando para o armazenamento
    CNG ("Key Storage Provider"). Com a chave no CNG, o PJeOffice (aba "Windows") falha ao
    assinar o desafio de login do PDPJ, que usa MD5WITHRSA: "Unrecognised hash algorithm".
    Sem o atributo, o Windows importa no provedor CryptoAPI classico, que assina MD5.
    O novo pacote usa uma senha aleatoria descartavel e cifra 3DES/SHA1, aceita por qualquer Windows.
    """
    import secrets

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import PrivateFormat

    try:
        key, cert, chain = pkcs12.load_key_and_certificates(data, password.encode("utf-8"))
    except ValueError as exc:
        raise CertificateError("Senha incorreta ou arquivo nao e um certificado A1 (.pfx/.p12)") from exc
    temp_password = secrets.token_urlsafe(24)
    encryption = (
        PrivateFormat.PKCS12.encryption_builder()
        .kdf_rounds(50000)
        .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
        .hmac_hash(hashes.SHA1())
        .build(temp_password.encode("ascii"))
    )
    rewrapped = pkcs12.serialize_key_and_certificates(None, key, cert, chain or None, encryption)
    return rewrapped, temp_password


_IMPORT_PS = r"""
$ErrorActionPreference = 'Stop'
$b64 = [Console]::In.ReadLine()
$pw  = [Console]::In.ReadLine()
$bytes = [Convert]::FromBase64String($b64)
$flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]'UserKeySet,PersistKeySet'
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($bytes, $pw, $flags)
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store('My','CurrentUser')
$store.Open('ReadWrite')
$store.Add($cert)
$store.Close()
Write-Output $cert.Thumbprint
# Chaves CNG nao expoem CspKeyContainerInfo no .NET Framework (PrivateKey nulo ou excecao).
try { Write-Output ("PROVIDER=" + $cert.PrivateKey.CspKeyContainerInfo.ProviderName) } catch { Write-Output "PROVIDER=CNG" }
"""

_REMOVE_PS = r"""
$ErrorActionPreference = 'Stop'
$t = [Console]::In.ReadLine().Trim()
$path = "Cert:\CurrentUser\My\$t"
if (Test-Path $path) { Remove-Item -Path $path -DeleteKey }
Write-Output 'OK'
"""


class WindowsCertificateStore:
    """Importa o A1 no repositorio do usuario atual e remove ao final.

    A senha e o conteudo do .pfx sao enviados pelo stdin do PowerShell, nunca por
    argumento de linha de comando (que ficaria visivel na lista de processos).
    """

    def __init__(self, powershell: str = "powershell.exe") -> None:
        if platform.system() != "Windows":
            raise CertificateError("A instalacao temporaria do A1 exige Windows")
        self.powershell = powershell

    def install(self, data: bytes, password: str) -> str:
        legacy_data, legacy_password = _rewrap_for_legacy_csp(data, password)
        payload = base64.b64encode(legacy_data).decode("ascii") + "\n" + legacy_password + "\n"
        lines = [line.strip() for line in self._run(_IMPORT_PS, payload).strip().splitlines() if line.strip()]
        thumbprint = next((line.upper() for line in lines if len(line) == 40 and "=" not in line), "")
        provider = next((line.split("=", 1)[1] for line in lines if line.startswith("PROVIDER=")), "")
        if len(thumbprint) != 40:
            raise CertificateError("Importacao do certificado nao retornou thumbprint valido")
        # Chave CNG aparece sem provedor (PrivateKey nulo no .NET Framework) ou como "Key Storage".
        if not provider or provider == "CNG" or "key storage" in provider.lower():
            self.remove(thumbprint)
            raise CertificateError(
                "O Windows guardou a chave no armazenamento CNG, que o PJeOffice nao consegue usar "
                "para o login (MD5WITHRSA). Certificado removido; nada foi enviado."
            )
        return thumbprint

    def remove(self, thumbprint: str) -> None:
        self._run(_REMOVE_PS, thumbprint + "\n")

    def contains(self, thumbprint: str) -> bool:
        out = self._run("$t = [Console]::In.ReadLine().Trim(); Test-Path \"Cert:\\CurrentUser\\My\\$t\"", thumbprint + "\n")
        return out.strip().lower().endswith("true")

    def _run(self, script: str, stdin: str) -> str:
        completed = subprocess.run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            # stderr do PowerShell nao contem a senha (ela so trafega pelo stdin)
            raise CertificateError(f"Falha no repositorio de certificados: {completed.stderr.strip()[:300]}")
        return completed.stdout


class SimulatedCertificateStore:
    def __init__(self) -> None:
        self.installed: set[str] = set()

    def install(self, data: bytes, password: str) -> str:
        from cryptography.hazmat.primitives import hashes

        _key, cert, _ = pkcs12.load_key_and_certificates(data, password.encode("utf-8"))
        thumb = cert.fingerprint(hashes.SHA1()).hex().upper()
        self.installed.add(thumb)
        return thumb

    def remove(self, thumbprint: str) -> None:
        self.installed.discard(thumbprint)
