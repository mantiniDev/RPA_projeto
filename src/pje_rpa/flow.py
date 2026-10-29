from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable

from .audit import AuditLog
from .browser import BrowserError, BrowserPort, SimulatedBrowser
from .certificate import CertificateInfo
from .models import AppConfig, FlowState, LawyerRecord, RunResult, RunStatus, digits
from .signer import SignerError, SignerPort
from .totp import TotpKey

# Tempo maximo para o PDPJ sair da tela de login (ou do 2FA) depois da acao do usuario/PJeOffice.
LOGIN_TIMEOUT_MS = 90_000
# Codigo recusado (relogio fora de sincronia, codigo no limite, digitacao): tenta de novo.
SECOND_FACTOR_ATTEMPTS = 3

# Recebe a mensagem a exibir e devolve o codigo do segundo fator (ou None se expirar).
CodeProvider = Callable[[str], "str | None"]


class PJeRegistrationFlow:
    """Cadastro automatico usando o PJeOffice PRO instalado no computador.

    Paradas que continuam existindo:
    - CAPTCHA ou bloqueio de acesso: encerra (nao ha tentativa de contorno);
    - segundo fator: gera o codigo pela chave do autenticador (planilha) ou pede ao operador;
    - divergencia de nome/CPF/OAB entre certificado, dados e PJe: encerra antes de enviar.
    """

    def __init__(
        self,
        config: AppConfig,
        record: LawyerRecord,
        certificate: CertificateInfo,
        certificate_password: str,
        browser: BrowserPort,
        signer: SignerPort,
        audit: AuditLog,
        code_provider: CodeProvider,
        rehearsal: bool = False,
        totp: TotpKey | None = None,
    ) -> None:
        self.config = config
        self.record = record
        self.certificate = certificate
        self._password = certificate_password
        self.browser = browser
        self.signer = signer
        self.audit = audit
        self.code_provider = code_provider
        # Chave do autenticador (da planilha): gera o codigo do 2FA sem pedir ao operador.
        self.totp = totp
        # Ensaio: roda no PJe real, mas para antes de "Confirmar dados" (nada e enviado nem assinado).
        self.rehearsal = rehearsal
        self.state = FlowState.START

    def run(self) -> RunResult:
        try:
            return self._run()
        except (BrowserError, SignerError) as exc:
            return self._failed(str(exc))
        except Exception as exc:
            self.audit.record("unexpected_error", self.state.value, {"type": type(exc).__name__})
            return self._failed(f"Erro inesperado: {type(exc).__name__}")
        finally:
            self._password = ""

    def _run(self) -> RunResult:
        mismatch = self._certificate_mismatch()
        if mismatch:
            return self._blocked(mismatch)

        self._transition(FlowState.LOGIN, "open_login", {"tribunal": self.config.tribunal})
        self.browser.open(self.config.base_url)
        if blocked := self._blocked_reason():
            return self._blocked(blocked)
        # Tribunais com login centralizado (PDPJ/SSO) exigem um clique antes da tela do certificado.
        if "login_sso_button" in self.config.selectors and self.browser.visible("login_sso_button", 5000):
            self.browser.click("login_sso_button")
            self.audit.record("sso_login_opened", self.state.value)
        if not self.browser.visible("login_certificate_button", 10000):
            return self._failed("Botao de certificado digital nao localizado")

        self.browser.click("login_certificate_button")
        self._transition(FlowState.WAITING_CERTIFICATE, "certificate_requested")
        self.signer.authenticate(self.certificate, self._password)
        self.audit.record("signer_authenticated", self.state.value)

        # Depois do OK, o PJeOffice ainda assina e envia o desafio ao PDPJ (medido: ~20 s).
        # Fechar o navegador antes disso aborta o login, entao espera a tela de login sumir.
        if not self.browser.wait_hidden("login_certificate_button", LOGIN_TIMEOUT_MS):
            if blocked := self._blocked_reason():
                return self._blocked(blocked)
            return self._failed("Login por certificado nao concluiu: o PJe continuou na tela de login")
        self.audit.record("login_page_left", self.state.value)

        if blocked := self._blocked_reason():
            return self._blocked(blocked)

        if self.browser.visible("second_factor", 8000):
            self._transition(FlowState.SECOND_FACTOR, "second_factor_detected",
                             {"origem": "chave da planilha" if self.totp else "operador"})
            for attempt in range(1, SECOND_FACTOR_ATTEMPTS + 1):
                if self.totp:
                    code = self.totp.fresh_code()
                else:
                    code = self.code_provider(
                        "O PJe pediu o codigo de segundo fator (app autenticador do advogado)."
                        if attempt == 1 else "Codigo recusado. Informe o codigo atual do autenticador."
                    )
                if not code:
                    return self._needs_human("Codigo de segundo fator nao informado a tempo")
                self.browser.fill("second_factor_input", code.strip())
                self.browser.click("second_factor_submit")
                if isinstance(self.browser, SimulatedBrowser):
                    self.browser.human_completed_second_factor()
                last = attempt == SECOND_FACTOR_ATTEMPTS
                if self.browser.wait_hidden("second_factor_input", LOGIN_TIMEOUT_MS if last else 15_000):
                    break
                self.audit.record("second_factor_rejected", self.state.value, {"tentativa": attempt})
                if self.totp:
                    # Relogio do PC adiantado/atrasado ou codigo no limite: o proximo codigo resolve.
                    time.sleep(self.totp.seconds_left() + 0.5)
            else:
                return self._needs_human("Codigo de segundo fator recusado ou expirado")
            self.audit.record("second_factor_accepted", self.state.value, {"tentativa": attempt})
            if blocked := self._blocked_reason():
                return self._blocked(blocked)

        if self.browser.visible("lawyer_panel", 2500):
            self._transition(FlowState.ALREADY_REGISTERED, "existing_profile_detected")
            return self._result(RunStatus.ALREADY_REGISTERED, "Advogado ja cadastrado neste PJe")

        if not self.browser.visible("registration_form", 10000):
            return self._failed("Formulario de cadastro nao localizado apos autenticacao")
        self._transition(FlowState.REGISTRATION_FORM, "registration_form_detected")
        if mismatch := self._identity_mismatch():
            return self._blocked(mismatch)

        self._fill_form()
        self._transition(FlowState.REVIEW, "form_filled", self.record.safe_summary())

        if self.rehearsal:
            # Estrutura da tela ja preenchida: mostra campos que sobraram (ex.: selects) para homologar.
            self.audit.record("rehearsal_stopped_before_submit", self.state.value,
                              {"page": self._scrub(self.browser.outline())})
            return self._result(
                RunStatus.REHEARSAL_OK,
                "Ensaio concluido: login, identidade e formulario validados; nada foi enviado nem assinado",
            )

        self.browser.click("confirm_data")
        if blocked := self._blocked_reason():
            return self._blocked(blocked)
        if not self.browser.visible("commitment_term", 10000):
            return self._failed("Termo de compromisso nao localizado")
        self._transition(FlowState.COMMITMENT_TERM, "commitment_term_detected",
                         {"autorizacao_ref": self.record.autorizacao_ref})

        self.browser.click("sign_term")
        self.signer.sign(self.certificate, self._password)
        self.audit.record("term_signed_via_pjeoffice", self.state.value)

        if not self.browser.visible("completion", 15000):
            blocked = self._blocked_reason()
            return self._blocked(blocked) if blocked else self._failed("Conclusao do cadastro nao confirmada")
        self._transition(FlowState.COMPLETED, "registration_completed")
        return self._result(RunStatus.COMPLETED, "Cadastro concluido e confirmado pela tela final")

    def _certificate_mismatch(self) -> str | None:
        if _normalize_name(self.certificate.nome) != _normalize_name(self.record.nome_esperado):
            return "Nome do certificado diferente do nome informado"
        if self.certificate.cpf != self.record.cpf_esperado:
            return "CPF do certificado diferente do CPF informado"
        self.audit.record("certificate_validated", self.state.value, self.certificate.safe_summary())
        return None

    def _fill_form(self) -> None:
        address = self.record.endereco
        filled, missing = [], []
        # CEP primeiro: o PJe busca o endereco pelo CEP e sobrescreveria o que viesse antes.
        if self.browser.visible("cep", 800):
            self.browser.fill("cep", address.cep)
            self.browser.commit("cep")
            filled.append("cep")
        else:
            missing.append("cep")
        phone = self.record.telefone
        # Tribunais que separam DDD e numero (ex.: TJAP cmpDDD + cmpTel) tem o grupo phone_ddd.
        if "phone_ddd" in self.config.selectors and self.browser.visible("phone_ddd", 800):
            self.browser.fill("phone_ddd", phone[:2])
            filled.append("phone_ddd")
            phone = phone[2:]
        fields = {
            "email": self.record.email,
            "phone": phone,
            "logradouro": address.logradouro,
            "address_number": address.numero,
            "complemento": address.complemento,
            "bairro": address.bairro,
            "cidade": address.cidade,
            "uf": address.uf,
        }
        for group, value in fields.items():
            if not value:
                continue
            if self.browser.visible(group, 800):
                self.browser.fill(group, value)
                filled.append(group)
            else:
                missing.append(group)  # alguns tribunais preenchem cidade/UF pelo CEP
        self.audit.record("fields_filled", self.state.value, {"fields": filled, "nao_encontrados": missing})

    def _identity_mismatch(self) -> str | None:
        # Campo dedicado quando o tribunal tem (seletor homologado); senao, o PJe exibe nome/CPF
        # como texto fixo (ex.: TJAP "Cadastro de novo usuario") e a conferencia e feita no texto.
        page = None
        methods = {}

        def source(group: str) -> str | None:
            nonlocal page
            if self.browser.visible(group, 2000):
                methods[group] = "campo"
                return self.browser.read(group)
            methods[group] = "texto da tela"
            if page is None:
                page = self.browser.page_text()
            return None

        detected_name = source("detected_name")
        if detected_name is not None:
            name_ok = _normalize_name(detected_name) == _normalize_name(self.record.nome_esperado)
        else:
            name_ok = _normalize_name(self.record.nome_esperado) in _normalize_name(page)
        if not name_ok:
            return "Divergencia entre o nome esperado e o retornado pelo PJe"

        detected_cpf = source("detected_cpf")
        cpfs = [digits(detected_cpf)] if detected_cpf is not None else [
            digits(m) for m in re.findall(r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}", page)]
        if self.record.cpf_esperado not in cpfs:
            return "Divergencia entre o CPF esperado e o retornado pelo PJe"

        detected_oab = source("detected_oab")
        if detected_oab is not None:
            detected_oab_digits = digits(detected_oab)
            detected_oab_letters = re.sub(r"[^A-Z]", "", detected_oab.upper()).replace("OAB", "")
            if detected_oab_digits != self.record.oab_numero or self.record.oab_uf not in detected_oab_letters:
                return "Divergencia entre a OAB esperada e a retornada pelo PJe"
        elif self.record.oab_numero not in {digits(m) for m in re.findall(r"\d[\d.]*", page)}:
            # A tela de novo usuario do PJe 1o grau nao mostra a OAB; o PJe confere a inscricao
            # no CNA depois, e Jus Postulandi continua sendo parada (stop_markers).
            methods["detected_oab"] = "nao exibida nesta tela"
        self.audit.record("identity_validated", self.state.value, {**self.record.safe_summary(), "conferencia": methods})
        return None

    def _blocked_reason(self) -> str | None:
        labels = {
            "captcha": "CAPTCHA detectado; nenhuma tentativa de contorno foi realizada",
            "certificate_error": "Erro ou bloqueio de certificado detectado",
            "access_blocked": "Bloqueio de acesso detectado",
            "jus_postulandi": "PJe nao reconheceu o perfil como advogado e indicou Jus Postulandi",
        }
        for group, reason in labels.items():
            marker = self.browser.page_text_contains(self.config.stop_markers.get(group, []))
            if marker:
                self.audit.record("stop_marker_detected", self.state.value, {"group": group, "marker": marker})
                return reason
        return None

    def _transition(self, state: FlowState, event: str, details: dict | None = None) -> None:
        self.state = state
        self.audit.record(event, state.value, details)

    def _blocked(self, reason: str) -> RunResult:
        self._transition(FlowState.BLOCKED, "flow_blocked", {"reason": reason})
        return self._result(RunStatus.BLOCKED, reason)

    def _failed(self, reason: str) -> RunResult:
        details: dict = {"reason": reason}
        if self.rehearsal:
            # No ensaio, registra a estrutura da tela (titulos, rotulos, botoes; nunca valores
            # digitados) para homologar os seletores da etapa que falhou.
            details["page"] = self._scrub(self.browser.outline())
        self._transition(FlowState.FAILED, "flow_failed", details)
        return self._result(RunStatus.FAILED, reason)

    def _scrub(self, value):
        """Mascara nome do titular e sequencias numericas (CPF, OAB, telefone) em textos da tela.

        Preserva maiusculas/minusculas: ids e names dos campos sao usados como seletores CSS.
        """
        if isinstance(value, dict):
            return {k: v if k in {"url", "tag", "type", "id", "name"} else self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        if not isinstance(value, str):
            return value
        text = re.sub(r"\d[\d.\-/ ]{3,}\d", "***", value)
        for part in _normalize_name(self.record.nome_esperado).split():
            if len(part) > 2:
                text = re.sub(_accent_insensitive(part), "***", text, flags=re.IGNORECASE)
        return text

    def _needs_human(self, reason: str) -> RunResult:
        self.audit.record("human_action_pending", self.state.value, {"reason": reason})
        return self._result(RunStatus.NEEDS_HUMAN, reason)

    def _result(self, status: RunStatus, reason: str) -> RunResult:
        return RunResult(
            run_id=self.audit.run_id,
            status=status,
            state=self.state,
            reason=reason,
            details={"tribunal": self.config.tribunal},
        )


def console_code_provider(message: str) -> str | None:
    print(f"\nSEGUNDO FATOR\n{message}\n")
    code = input("Codigo (Enter vazio cancela): ").strip()
    return code or None


_ACCENTS = {"A": "AÁÀÂÃÄ", "E": "EÉÈÊË", "I": "IÍÌÎÏ", "O": "OÓÒÔÕÖ", "U": "UÚÙÛÜ", "C": "CÇ", "N": "NÑ"}


def _accent_insensitive(word: str) -> str:
    """Regex que casa a palavra com ou sem acento (JOAO casa João)."""
    return "".join(f"[{_ACCENTS[ch]}]" if ch in _ACCENTS else re.escape(ch) for ch in word.upper())


def _normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(ascii_text.upper().split())
