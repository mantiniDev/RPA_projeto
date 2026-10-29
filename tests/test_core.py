from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pje_rpa.audit import AuditLog, verify_audit_file
from pje_rpa.browser import SimulatedBrowser
from pje_rpa.certificate import CertificateInfo, read_pfx, CertificateError
from pje_rpa.signer import SignerError, SimulatedSigner, choose_row
from datetime import datetime, timezone
from dataclasses import replace
from pje_rpa.config import (
    ConfigurationError,
    assert_live_ready,
    assert_rehearsal_ready,
    load_config,
    load_lawyer,
)
from pje_rpa.flow import PJeRegistrationFlow
from pje_rpa.models import RunStatus


def _info(record):
    return CertificateInfo(record.nome_esperado, record.cpf_esperado, "AC SIMULADA",
                           datetime(2099, 1, 1, tzinfo=timezone.utc), "0" * 40)


def _flow(config, record, browser, audit, code="000000", rehearsal=False):
    return PJeRegistrationFlow(config, record, _info(record), "senha", browser,
                               SimulatedSigner(browser), audit, code_provider=lambda m: code,
                               rehearsal=rehearsal)


PROJECT = Path(__file__).resolve().parents[1]


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(PROJECT / "config.example.toml")
        self.record = load_lawyer(PROJECT / "cadastro.example.json")

    def test_example_files_are_valid_for_simulation(self) -> None:
        self.assertEqual(self.config.environment, "simulation")
        self.assertEqual(len(self.record.cpf_esperado), 11)

    def test_live_is_fail_closed_by_default(self) -> None:
        with self.assertRaises(ConfigurationError):
            assert_live_ready(self.config, self.record)

    def test_success_simulation_and_audit_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audit = AuditLog(Path(temp))
            browser = SimulatedBrowser(self.record, "success")
            flow = _flow(self.config, self.record, browser, audit)
            result = flow.run()
            self.assertEqual(result.status, RunStatus.COMPLETED)
            ok, message = verify_audit_file(audit.path)
            self.assertTrue(ok, message)

    def test_captcha_stops_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audit = AuditLog(Path(temp))
            browser = SimulatedBrowser(self.record, "captcha")
            flow = _flow(self.config, self.record, browser, audit)
            result = flow.run()
            self.assertEqual(result.status, RunStatus.BLOCKED)
            self.assertIn("CAPTCHA", result.reason)

    def test_jus_postulandi_stops_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audit = AuditLog(Path(temp))
            browser = SimulatedBrowser(self.record, "jus_postulandi")
            flow = _flow(self.config, self.record, browser, audit)
            result = flow.run()
            self.assertEqual(result.status, RunStatus.BLOCKED)
            self.assertIn("Jus Postulandi", result.reason)

    def test_audit_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audit = AuditLog(Path(temp))
            audit.record("one", "START", {"cpf": "52998224725"})
            contents = audit.path.read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", contents)
            audit.path.write_text(contents.replace("START", "CHANGED"), encoding="utf-8")
            ok, _ = verify_audit_file(audit.path)
            self.assertFalse(ok)

    def test_rehearsal_stops_before_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audit = AuditLog(Path(temp))
            browser = SimulatedBrowser(self.record, "success")
            signer = SimulatedSigner(browser)
            flow = PJeRegistrationFlow(self.config, self.record, _info(self.record), "senha", browser,
                                       signer, audit, code_provider=lambda m: None, rehearsal=True)
            result = flow.run()
            self.assertEqual(result.status, RunStatus.REHEARSAL_OK)
            self.assertEqual(browser.stage, "registration")  # nao clicou em confirmar
            self.assertNotIn("sign", signer.calls)
            self.assertIn("rehearsal_stopped_before_submit", audit.path.read_text(encoding="utf-8"))

    def test_second_factor_uses_provided_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audit = AuditLog(Path(temp))
            browser = SimulatedBrowser(self.record, "second_factor")
            result = _flow(self.config, self.record, browser, audit, code="123456").run()
            self.assertEqual(result.status, RunStatus.COMPLETED)
            self.assertEqual(browser.values["second_factor_input"], "123456")

    def test_second_factor_without_code_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audit = AuditLog(Path(temp))
            browser = SimulatedBrowser(self.record, "second_factor")
            result = _flow(self.config, self.record, browser, audit, code=None).run()
            self.assertEqual(result.status, RunStatus.NEEDS_HUMAN)

    def test_already_registered(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            audit = AuditLog(Path(temp))
            browser = SimulatedBrowser(self.record, "already_registered")
            result = _flow(self.config, self.record, browser, audit).run()
            self.assertEqual(result.status, RunStatus.ALREADY_REGISTERED)

    def test_rehearsal_skips_approval_flags_but_keeps_official_domain(self) -> None:
        record = replace(self.record, nome_esperado="FULANO DA SILVA", email="fulano@adv.com.br")
        official = replace(self.config, environment="production",
                           base_url="https://pje.trt2.jus.br/primeirograu/login.seam")
        assert_rehearsal_ready(official, record)  # sem selectors_approved/allow_live_submission
        with self.assertRaises(ConfigurationError):
            assert_live_ready(official, record)
        with self.assertRaises(ConfigurationError):
            assert_rehearsal_ready(replace(official, base_url="https://pje.trt2.com.br/login"), record)
        with self.assertRaises(ConfigurationError):
            assert_rehearsal_ready(self.config, record)  # environment = simulation

    def test_rehearsal_failure_records_scrubbed_page_outline(self) -> None:
        class FailingBrowser(SimulatedBrowser):
            def visible(self, group, timeout_ms=None):
                return False if group == "registration_form" else super().visible(group, timeout_ms)

            def outline(self):
                return {"url": "pje.trt2.jus.br/primeirograu/Painel", "headings": [
                    f"Bem-vindo, {self.record.nome_esperado} - CPF 529.982.247-25"]}

        with tempfile.TemporaryDirectory() as temp:
            audit = AuditLog(Path(temp))
            browser = FailingBrowser(self.record, "success")
            result = _flow(self.config, self.record, browser, audit, rehearsal=True).run()
            self.assertEqual(result.status, RunStatus.FAILED)
            log = audit.path.read_text(encoding="utf-8")
            self.assertIn("primeirograu/Painel", log)
            self.assertIn("BEM-VINDO", log)
            page = log.split('"page"')[1]
            self.assertNotIn("ADVOGADO", page)
            self.assertNotIn("529", page)

    def test_sso_button_is_clicked_when_configured(self) -> None:
        class SsoBrowser(SimulatedBrowser):
            def open(self, url):
                self.stage = "sso"

            def visible(self, group, timeout_ms=None):
                if self.stage == "sso":
                    return group == "login_sso_button"
                return super().visible(group, timeout_ms)

            def click(self, group):
                if self.stage == "sso" and group == "login_sso_button":
                    self.stage = "login"
                    return
                super().click(group)

        config = replace(self.config, selectors={**self.config.selectors, "login_sso_button": ["text=PDPJ"]})
        with tempfile.TemporaryDirectory() as temp:
            audit = AuditLog(Path(temp))
            result = _flow(config, self.record, SsoBrowser(self.record, "success"), audit).run()
            self.assertEqual(result.status, RunStatus.COMPLETED)
            self.assertIn("sso_login_opened", audit.path.read_text(encoding="utf-8"))

    def test_waits_for_slow_certificate_login(self) -> None:
        # O PJeOffice leva ~20 s para enviar a assinatura depois do OK; o fluxo nao pode desistir antes.
        import time as _time

        class SlowLogin(SimulatedBrowser):
            def human_completed_certificate(self):
                self._done_at = _time.monotonic() + 1.5
                self._target = "registration"

            def visible(self, group, timeout_ms=None):
                if getattr(self, "_done_at", None) and _time.monotonic() >= self._done_at:
                    self.stage, self._done_at = self._target, None
                if self.stage == "post_auth" and getattr(self, "_done_at", None):
                    return group == "login_certificate_button"
                return super().visible(group, timeout_ms)

        with tempfile.TemporaryDirectory() as temp:
            audit = AuditLog(Path(temp))
            result = _flow(self.config, self.record, SlowLogin(self.record, "success"), audit).run()
            self.assertEqual(result.status, RunStatus.COMPLETED)
            self.assertIn("login_page_left", audit.path.read_text(encoding="utf-8"))

    def test_login_that_never_leaves_login_page_fails_clearly(self) -> None:
        import pje_rpa.flow as flow_module

        class StuckLogin(SimulatedBrowser):
            def visible(self, group, timeout_ms=None):
                return group == "login_certificate_button"

            def click(self, group):
                pass

        original = flow_module.LOGIN_TIMEOUT_MS
        flow_module.LOGIN_TIMEOUT_MS = 1200
        try:
            with tempfile.TemporaryDirectory() as temp:
                result = _flow(self.config, self.record, StuckLogin(self.record, "success"), AuditLog(Path(temp))).run()
        finally:
            flow_module.LOGIN_TIMEOUT_MS = original
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertIn("tela de login", result.reason)


class ChooseRowTests(unittest.TestCase):
    def _cert(self, issuer="AC Certisign RFB G5", expiry=datetime(2030, 6, 1, 15, 0, 0, tzinfo=timezone.utc)):
        return CertificateInfo("FULANO DE TAL SILVA", "52998224725", issuer, expiry, "A" * 40)

    def _row(self, name, issuer, expiry):
        return ["VIRTUAL: mscapi", name, issuer, expiry.astimezone().strftime("%d/%m/%Y %H:%M:%S")]

    def test_picks_same_name_issuer_and_expiry_among_homonyms(self) -> None:
        cert = self._cert()
        rows = [
            self._row("OUTRA PESSOA", "AC Certisign RFB G5", cert.valido_ate),
            self._row("FULANO DE TAL SILVA", "AC OAB G3", datetime(2028, 9, 3, tzinfo=timezone.utc)),
            self._row("FULANO DE TAL SILVA", "AC Certisign RFB G5", cert.valido_ate),
            self._row("FULANO DE TAL SILVA", "AC OAB G3", datetime(2026, 3, 9, tzinfo=timezone.utc)),
        ]
        self.assertEqual(choose_row(rows, cert), 2)

    def test_refuses_when_only_other_certificates_of_same_person_exist(self) -> None:
        cert = self._cert()
        rows = [self._row("FULANO DE TAL SILVA", "AC OAB G3", datetime(2028, 9, 3, tzinfo=timezone.utc))]
        with self.assertRaises(SignerError):
            choose_row(rows, cert)

    def test_refuses_duplicates(self) -> None:
        cert = self._cert()
        row = self._row("FULANO DE TAL SILVA", "AC Certisign RFB G5", cert.valido_ate)
        with self.assertRaises(SignerError):
            choose_row([row, list(row)], cert)


class WebAppOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        import threading
        from pje_rpa.webapp import RegistrationServer

        self.server = RegistrationServer(PROJECT / "config.example.toml", Path(tempfile.mkdtemp()),
                                         port=0, rehearsal=True)
        self.port = self.server._httpd.server_address[1]
        self.server._allowed_hosts = {f"{h}:{self.port}" for h in ("127.0.0.1", "localhost", "[::1]")}
        threading.Thread(target=self.server._httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server._httpd.shutdown()
        self.server._httpd.server_close()

    def _request(self, method, path, headers=None, body=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def test_page_renders_in_rehearsal_mode(self) -> None:
        status, body = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'const MODE="ensaio"', body)

    def test_foreign_origin_is_rejected(self) -> None:
        status, _ = self._request("POST", "/code", {"Origin": "https://site-malicioso.com"}, b'{"code":"1"}')
        self.assertEqual(status, 403)

    def test_post_without_origin_is_rejected(self) -> None:
        status, _ = self._request("POST", "/code", {}, b'{"code":"1"}')
        self.assertEqual(status, 403)

    def test_rebound_host_is_rejected(self) -> None:
        status, _ = self._request("GET", "/", {"Host": f"atacante.com:{self.port}"})
        self.assertEqual(status, 403)

    def test_same_origin_code_is_accepted_once(self) -> None:
        origin = {"Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self._request("POST", "/code", origin, b'{"code":"1"}')[0], 200)
        self.assertEqual(self._request("POST", "/code", origin, b'{"code":"2"}')[0], 409)


if __name__ == "__main__":
    unittest.main()



def _xlsx(rows, links=None):
    """Monta um .xlsx minimo em memoria: textos inline e numeros como celula numerica.

    links: {"P2": "file:///C:/a.pfx"} vira hyperlink externo na celula.
    """
    import io
    import zipfile
    from xml.sax.saxutils import escape

    def cell(ref, value):
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"><v>{value}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'

    body = "".join(
        f'<row r="{r}">' + "".join(cell(f"{chr(65 + c)}{r}", v) for c, v in enumerate(row)) + "</row>"
        for r, row in enumerate(rows, start=1))
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", f'<workbook {ns} {rel}><sheets><sheet name="A" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels",
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        links = links or {}
        hyperlinks = "".join(f'<hyperlink ref="{ref}" r:id="rL{i}"/>' for i, ref in enumerate(links))
        archive.writestr("xl/worksheets/sheet1.xml",
                         f"<worksheet {ns} {rel}><sheetData>{body}</sheetData>"
                         + (f"<hyperlinks>{hyperlinks}</hyperlinks>" if links else "") + "</worksheet>")
        if links:
            archive.writestr("xl/worksheets/_rels/sheet1.xml.rels",
                             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                             + "".join(f'<Relationship Id="rL{i}" Type="hyperlink" Target="{escape(target)}" '
                                       'TargetMode="External"/>' for i, target in enumerate(links.values()))
                             + "</Relationships>")
    return buffer.getvalue()


_HEADER = ["Nome do titular", "CPF", "OAB numero", "OAB UF", "E-mail", "Telefone", "CEP", "Logradouro",
           "Número", "Complemento", "Bairro", "Cidade", "UF", "Referência da autorização"]


class SheetTests(unittest.TestCase):
    def test_modelo_csv_matches_example_json(self) -> None:
        from pje_rpa.planilha import read_sheet

        rows = read_sheet("cadastro.modelo.csv", (PROJECT / "cadastro.modelo.csv").read_bytes())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].linha, 2)
        self.assertEqual(rows[0].erros, [])
        from pje_rpa.config import lawyer_from_dict
        self.assertEqual(lawyer_from_dict(rows[0].cadastro), load_lawyer(PROJECT / "cadastro.example.json"))

    def test_xlsx_restores_lost_zeros_and_splits_oab(self) -> None:
        from pje_rpa.planilha import read_sheet

        header = [h for h in _HEADER if h != "OAB UF"]
        data = _xlsx([header,
                      ["MARIA DA SILVA", 1234567890, "98.765/sp", "maria@adv.com.br", 11987654321, 1310100,
                       "AV PAULISTA", 1000, "", "BELA VISTA", "SAO PAULO", "SP", "titular presente"],
                      [],
                      ["JOSE SOUZA", 11111111111, "", "x", 1, 1, "", "", "", "", "", "", ""]])
        rows = read_sheet("advogados.xlsx", data)
        self.assertEqual([r.linha for r in rows], [2, 4])
        ok = rows[0]
        self.assertEqual(ok.erros, [])
        self.assertEqual(ok.cadastro["cpf_esperado"], "01234567890")
        self.assertEqual(ok.cadastro["endereco"]["cep"], "01310100")
        self.assertEqual((ok.cadastro["oab_numero"], ok.cadastro["oab_uf"]), ("98.765", "SP"))
        self.assertIn("cpf_esperado invalido", rows[1].erros)
        self.assertIn("oab_numero obrigatorio", rows[1].erros)

    def test_certificate_path_and_password_from_csv(self) -> None:
        from pje_rpa.planilha import read_sheet

        folder = Path(tempfile.mkdtemp())
        pfx = folder / "meu certificado.pfx"
        pfx.write_bytes(b"x")
        text = (f"Nome;CPF;Certificado;Senha do certificado\n"
                f'MARIA DA SILVA;52998224725;"{pfx}"; s3nha com espaco \n'
                f"MARIA DA SILVA;52998224725;{pfx.as_uri()};x\n"
                f"MARIA DA SILVA;52998224725;{folder / 'falta.pfx'};x\n"
                f"MARIA DA SILVA;52998224725;meu certificado.pfx;x\n"
                f"MARIA DA SILVA;52998224725;https://exemplo.com/a.pfx;x\n")
        rows = read_sheet("a.csv", text.encode("utf-8"))
        self.assertEqual(rows[0].certificado, str(pfx))
        self.assertEqual(rows[0].senha, " s3nha com espaco ")
        self.assertNotIn("s3nha", repr(rows[0]))
        self.assertEqual(rows[1].certificado, str(pfx))
        self.assertIn("certificado nao encontrado", rows[2].erros[-1])
        self.assertIn("caminho completo", rows[3].erros[-1])
        self.assertIn("nao um link da internet", rows[4].erros[-1])
        # caminho relativo funciona quando a pasta da planilha e conhecida (CLI)
        relative = read_sheet("a.csv", text.encode("utf-8"), base_dir=folder)
        self.assertEqual(relative[3].certificado, str(pfx))

    def test_xlsx_hyperlink_target_wins_over_display_text(self) -> None:
        from pje_rpa.planilha import read_sheet

        pfx = Path(tempfile.mkdtemp()) / "a1.pfx"
        pfx.write_bytes(b"x")
        data = _xlsx([["Nome", "CPF", "Link do certificado", "Senha"],
                      ["MARIA DA SILVA", 52998224725, "clique aqui", 12345678]],
                     links={"C2": pfx.as_uri()})
        row = read_sheet("a.xlsx", data)[0]
        self.assertEqual(row.certificado, str(pfx))
        self.assertEqual(row.senha, "12345678")

    def test_csv_cp1252_with_commas(self) -> None:
        from pje_rpa.planilha import read_sheet

        text = ",".join(_HEADER) + "\nJOÃO PEREIRA,529.982.247-25,123,BA,j@adv.com,(71) 99999-9999,40000-000,RUA A,1,,CENTRO,SALVADOR,BA,ref\n"
        rows = read_sheet("a.csv", text.encode("cp1252"))
        self.assertEqual(rows[0].erros, [])
        self.assertEqual(rows[0].cadastro["nome_esperado"], "JOÃO PEREIRA")

    def test_cli_loads_record_from_sheet_line(self) -> None:
        from pje_rpa.cli import _load_record

        record = _load_record(PROJECT / "cadastro.modelo.csv", None)
        self.assertEqual(record.cpf_esperado, "52998224725")
        with self.assertRaisesRegex(ConfigurationError, "Linha 5"):
            _load_record(PROJECT / "cadastro.modelo.csv", 5)

    def test_page_never_receives_password_but_start_uses_it(self) -> None:
        import base64
        from pje_rpa.webapp import RegistrationServer

        server = RegistrationServer(PROJECT / "config.example.toml", Path(tempfile.mkdtemp()), port=0)
        self.addCleanup(server._httpd.server_close)
        pfx = Path(tempfile.mkdtemp()) / "a1.pfx"
        pfx.write_bytes(b"conteudo-pfx")
        text = f"Nome;CPF;Certificado;Senha\nMARIA DA SILVA;52998224725;{pfx};segredo-da-planilha\n"
        body = json.dumps({"nome": "a.csv", "base64": base64.b64encode(text.encode()).decode()}).encode()

        status, answer = server.load_sheet(body)
        self.assertEqual(status, 200)
        self.assertNotIn("segredo-da-planilha", json.dumps(answer))
        self.assertEqual((answer["linhas"][0]["certificado"], answer["linhas"][0]["tem_senha"]), (str(pfx), True))

        self.assertEqual(server.credentials({"planilha_linha": 2}), (b"conteudo-pfx", "segredo-da-planilha"))
        # o que for digitado/arrastado na pagina substitui a planilha
        manual = {"planilha_linha": 2, "password": "outra", "pfx_base64": base64.b64encode(b"outro").decode()}
        self.assertEqual(server.credentials(manual), (b"outro", "outra"))
        with self.assertRaisesRegex(ValueError, "senha"):
            server.credentials({"pfx_base64": base64.b64encode(b"outro").decode()})

        status, answer = server.load_sheet(json.dumps({"nome": "a.pdf", "base64": ""}).encode())
        self.assertEqual(status, 400)
        self.assertIn("xlsx", answer["error"])
