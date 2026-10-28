from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .audit import AuditLog, verify_audit_file
from .browser import SimulatedBrowser
from .config import ConfigurationError, assert_live_ready, lawyer_from_dict, load_config, load_lawyer
from .flow import PJeRegistrationFlow, console_code_provider
from .models import LawyerRecord
from .planilha import read_sheet


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RPA assistido para cadastro inicial de advogado no PJe")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Valida configuracao e cadastro sem abrir o navegador")
    _common_files(validate)

    simulate = sub.add_parser("simulate", help="Executa o fluxo sem acessar o PJe")
    _common_files(simulate)
    simulate.add_argument(
        "--scenario",
        choices=["success", "captcha", "jus_postulandi", "second_factor", "already_registered"],
        default="success",
    )
    simulate.add_argument("--audit-root", type=Path, default=Path("runtime/audit"))
    simulate.add_argument("--ensaio", action="store_true", help="Para antes de confirmar os dados")

    live = sub.add_parser("live", help="Executa no dominio oficial configurado")
    _common_files(live)
    live.add_argument("--audit-root", type=Path, default=Path("runtime/audit"))

    verify = sub.add_parser("verify-audit", help="Verifica a integridade encadeada de um audit.jsonl")
    verify.add_argument("path", type=Path)

    serve = sub.add_parser("serve", help="Abre a pagina local de upload do A1 (modo real)")
    serve.add_argument("--config", type=Path, default=Path("config.toml"))
    serve.add_argument("--audit-root", type=Path, default=Path("runtime/audit"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8723)
    serve.add_argument("--ensaio", action="store_true",
                       help="Entra no PJe real e confere os dados, mas para antes de enviar/assinar")

    inspect = sub.add_parser("inspect-signer", help="Lista os controles das janelas do PJeOffice abertas")
    inspect.add_argument("--config", type=Path, default=Path("config.toml"))

    sheet = sub.add_parser("planilha", help="Confere uma planilha de cadastro (.xlsx/.csv) linha a linha")
    sheet.add_argument("path", type=Path)
    return parser


def _common_files(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--data", type=Path, default=Path("cadastro.json"),
                        help="cadastro.json ou planilha .xlsx/.csv (com --linha)")
    parser.add_argument("--linha", type=int, help="Linha da planilha (como aparece no Excel)")


def _load_record(path: Path, linha: int | None) -> LawyerRecord:
    if path.suffix.lower() not in {".xlsx", ".csv"}:
        return load_lawyer(path)
    rows = read_sheet(path.name, path.read_bytes())
    if linha is None:
        if len(rows) != 1:
            raise ConfigurationError(f"A planilha tem {len(rows)} advogados: informe --linha")
        linha = rows[0].linha
    row = next((r for r in rows if r.linha == linha), None)
    if row is None:
        raise ConfigurationError(f"Linha {linha} vazia ou inexistente na planilha")
    return lawyer_from_dict(row.cadastro)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify-audit":
            ok, message = verify_audit_file(args.path)
            print(json.dumps({"valid": ok, "message": message}, ensure_ascii=False))
            return 0 if ok else 2

        if args.command == "serve":
            if args.host not in {"127.0.0.1", "localhost", "::1"}:
                raise ConfigurationError("A pagina local so pode escutar em 127.0.0.1")
            from .webapp import RegistrationServer
            RegistrationServer(args.config, args.audit_root, args.host, args.port,
                               rehearsal=args.ensaio).serve_forever()
            return 0
        if args.command == "inspect-signer":
            from .signer import dump_signer_windows
            print(dump_signer_windows(load_config(args.config).signer))
            return 0
        if args.command == "planilha":
            rows = read_sheet(args.path.name, args.path.read_bytes())
            report = []
            for row in rows:
                entry = {"linha": row.linha, "valida": not row.erros}
                if row.erros:
                    entry["erros"] = row.erros
                else:
                    entry["dados"] = lawyer_from_dict(row.cadastro).safe_summary()
                report.append(entry)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if all(not row.erros for row in rows) else 2

        config = load_config(args.config)
        record = _load_record(args.data, args.linha)
        if args.command == "validate":
            print(json.dumps({"valid": True, "tribunal": config.tribunal, "data": record.safe_summary()}, ensure_ascii=False, indent=2))
            return 0

        if args.command != "simulate":
            raise ConfigurationError("O modo real agora roda pela pagina local: use 'pje-rpa serve'")

        args.audit_root.mkdir(parents=True, exist_ok=True)
        audit = AuditLog(args.audit_root)
        from .browser import SimulatedBrowser
        from .certificate import CertificateInfo
        from .signer import SimulatedSigner
        from datetime import datetime, timezone
        browser = SimulatedBrowser(record, scenario=args.scenario)
        info = CertificateInfo(record.nome_esperado, record.cpf_esperado, "AC SIMULADA",
                               datetime(2099,1,1,tzinfo=timezone.utc), "0"*40)
        signer = SimulatedSigner(browser)

        try:
            flow = PJeRegistrationFlow(config, record, info, "simulada", browser, signer, audit,
                                       code_provider=lambda msg: "000000", rehearsal=args.ensaio)
            result = flow.run()
            serialized = asdict(result)
            serialized["status"] = result.status.value
            serialized["state"] = result.state.value
            result_path = audit.write_result(serialized)
            print(json.dumps({**serialized, "result_path": str(result_path)}, ensure_ascii=False, indent=2))
            return 0 if result.status.value in {"COMPLETED", "ALREADY_REGISTERED", "REHEARSAL_OK"} else 3
        finally:
            if browser is not None:
                browser.close()
    except (ConfigurationError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERRO DE CONFIGURACAO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

