"""Leitura dos dados do advogado a partir de uma planilha (.xlsx ou .csv).

Uma linha por advogado, com cabecalho na primeira linha. Os nomes das colunas sao os
mesmos campos da pagina local (acentos, maiusculas e espacos nao importam). A planilha
tambem pode trazer a senha do certificado e o caminho do .pfx no computador (texto ou
hyperlink do Excel). A senha fica so em memoria: nunca volta para a pagina nem para o log.

So usa a biblioteca padrao: o .xlsx e um zip de XML e lemos apenas a primeira aba.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

from .config import ConfigurationError, lawyer_from_dict

MAX_BYTES = 5 * 1024 * 1024
MAX_PFX_BYTES = 1024 * 1024
_MAX_XML_BYTES = 50 * 1024 * 1024
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# coluna normalizada -> campo do cadastro (os de endereco vao para "endereco")
_ALIASES = {
    "nome_esperado": {"nome", "nome_esperado", "nome_titular", "nome_do_titular", "nome_completo",
                      "nome_do_advogado", "nome_advogado", "advogado", "titular"},
    "cpf_esperado": {"cpf", "cpf_esperado", "cpf_do_titular", "cpf_titular"},
    "oab_numero": {"oab", "oab_numero", "numero_oab", "numero_da_oab", "n_oab", "no_oab", "inscricao_oab"},
    "oab_uf": {"oab_uf", "uf_oab", "uf_da_oab", "seccional", "seccional_oab"},
    "email": {"email", "e_mail"},
    "telefone": {"telefone", "celular", "fone", "telefone_celular"},
    "autorizacao_ref": {"autorizacao_ref", "autorizacao", "referencia_autorizacao",
                        "referencia_da_autorizacao", "referencia_da_autorizacao_do_advogado"},
    "cep": {"cep"},
    "logradouro": {"logradouro", "endereco", "rua"},
    "numero": {"numero", "numero_endereco", "n"},
    "complemento": {"complemento"},
    "bairro": {"bairro"},
    "cidade": {"cidade", "municipio"},
    "uf": {"uf", "estado", "uf_endereco"},
    "senha": {"senha", "senha_do_certificado", "senha_certificado", "senha_do_a1", "senha_a1", "senha_pfx"},
    "certificado": {"certificado", "certificado_a1", "arquivo_do_certificado", "caminho_do_certificado",
                    "link_do_certificado", "link_certificado", "local_do_certificado", "pfx", "arquivo_pfx",
                    "caminho_pfx"},
}
_COLUMN_TO_FIELD = {alias: name for name, aliases in _ALIASES.items() for alias in aliases}
_ADDRESS_FIELDS = {"cep", "logradouro", "numero", "complemento", "bairro", "cidade", "uf"}
_CERT_FIELDS = {"senha", "certificado"}


@dataclass
class SheetRow:
    linha: int
    cadastro: dict
    erros: list[str] = field(default_factory=list)
    certificado: str = ""  # caminho do .pfx ja resolvido, quando a planilha informa
    senha: str = field(default="", repr=False)

    def for_page(self) -> dict:
        """O que pode ir para a pagina: tudo menos a senha."""
        return {"linha": self.linha, "cadastro": self.cadastro, "erros": self.erros,
                "certificado": self.certificado, "tem_senha": bool(self.senha)}


def read_sheet(filename: str, data: bytes, base_dir: Path | None = None) -> list[SheetRow]:
    """Le a planilha e devolve uma entrada por linha preenchida, com os erros de cada uma.

    base_dir e a pasta da planilha, quando conhecida: resolve caminhos relativos do certificado.
    """
    if len(data) > MAX_BYTES:
        raise ConfigurationError("Planilha maior que 5 MB")
    name = filename.lower()
    links: dict[tuple[int, int], str] = {}
    if name.endswith(".xlsx"):
        table, links = _read_xlsx(data)
    elif name.endswith(".csv"):
        table = _read_csv(data)
    else:
        raise ConfigurationError("Use uma planilha .xlsx ou .csv")
    if not table:
        raise ConfigurationError("Planilha vazia")

    header = [_normalize(cell) for cell in table[0]]
    fields = [_COLUMN_TO_FIELD.get(col) for col in header]
    if "cpf_esperado" not in fields or "nome_esperado" not in fields:
        raise ConfigurationError("A planilha precisa das colunas Nome e CPF na primeira linha")

    rows: list[SheetRow] = []
    for index, values in enumerate(table[1:], start=2):
        if not any(str(v).strip() for v in values):
            continue
        raw: dict = {"endereco": {}}
        extra = {"senha": "", "certificado": ""}
        for column, (col, value) in enumerate(zip(fields, values)):
            if col is None:
                continue
            if col == "certificado":
                # hyperlink do Excel: vale o destino, nao o texto exibido
                extra[col] = links.get((index, column), str(value)).strip()
            elif col == "senha":
                extra[col] = str(value)  # sem strip: espaco pode fazer parte da senha
            else:
                target = raw["endereco"] if col in _ADDRESS_FIELDS else raw
                target[col] = str(value).strip()
        _fix_lost_zeros(raw)
        _split_oab(raw)
        row = SheetRow(linha=index, cadastro=raw, senha=extra["senha"])
        try:
            lawyer_from_dict(raw)
        except ConfigurationError as exc:
            row.erros = str(exc).split("; ")
        if extra["certificado"]:
            try:
                row.certificado = str(resolve_certificate(extra["certificado"], base_dir))
            except ConfigurationError as exc:
                row.erros.append(str(exc))
        rows.append(row)
    if not rows:
        raise ConfigurationError("A planilha nao tem nenhuma linha preenchida abaixo do cabecalho")
    return rows


def resolve_certificate(value: str, base_dir: Path | None = None) -> Path:
    """Aceita C:\\pasta\\a.pfx, "C:\\pasta\\a.pfx", file:///C:/pasta/a.pfx ou caminho relativo a planilha."""
    text = value.strip().strip('"').strip()
    if text.lower().startswith("file:"):
        parsed = urlparse(text)
        text = unquote(parsed.path)
        if parsed.netloc:  # \\servidor\pasta\a.pfx
            text = f"//{parsed.netloc}{text}"
        elif re.match(r"/[A-Za-z]:", text):
            text = text[1:]
    elif re.match(r"https?://", text, re.I):
        raise ConfigurationError("certificado: informe o caminho do .pfx no computador, nao um link da internet")
    path = Path(text)
    if not path.is_absolute():
        if base_dir is None:
            raise ConfigurationError(f"certificado: use o caminho completo (ex.: C:\\...\\{path.name})")
        path = base_dir / path
    if path.suffix.lower() not in {".pfx", ".p12"}:
        raise ConfigurationError(f"certificado: o arquivo precisa ser .pfx ou .p12 ({path.name})")
    if not path.is_file():
        raise ConfigurationError(f"certificado nao encontrado: {path}")
    if path.stat().st_size > MAX_PFX_BYTES:
        raise ConfigurationError(f"certificado: arquivo grande demais para um .pfx ({path.name})")
    return path


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _fix_lost_zeros(raw: dict) -> None:
    # O Excel guarda CPF e CEP como numero e some com o zero a esquerda. So completa o que
    # pode ter sido zero perdido (CEP comeca em 01000-000), para nao esconder digito faltando.
    for target, key, size, max_lost in ((raw, "cpf_esperado", 11, 3), (raw["endereco"], "cep", 8, 1)):
        value = target.get(key, "")
        if value.isdigit() and size - max_lost <= len(value) < size:
            target[key] = value.zfill(size)


def _split_oab(raw: dict) -> None:
    # Aceita "12345/BA" ou "BA 12345" numa coluna so.
    oab = raw.get("oab_numero", "")
    match = re.fullmatch(r"\s*(?:([A-Za-z]{2})\s*[-/ ]?\s*)?([\d.]+)\s*(?:[-/ ]\s*([A-Za-z]{2}))?\s*", oab)
    if match and (match.group(1) or match.group(3)):
        raw["oab_numero"] = match.group(2)
        raw.setdefault("oab_uf", "")
        if not raw["oab_uf"]:
            raw["oab_uf"] = (match.group(1) or match.group(3)).upper()


# ------------------------------------------------------------------------- CSV
def _read_csv(data: bytes) -> list[list[str]]:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    first = text.splitlines()[0] if text else ""
    delimiter = max(";,\t", key=first.count) if first else ";"
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


# ------------------------------------------------------------------------ XLSX
def _read_xlsx(data: bytes) -> "tuple[list[list[str]], dict[tuple[int, int], str]]":
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ConfigurationError("Arquivo .xlsx invalido") from exc
    with archive:
        if sum(info.file_size for info in archive.infolist()) > _MAX_XML_BYTES:
            raise ConfigurationError("Planilha grande demais depois de descompactada")
        shared = _shared_strings(archive)
        sheet_path = _first_sheet_path(archive)
        sheet = ElementTree.fromstring(archive.read(sheet_path))
        links = _hyperlinks(archive, sheet_path, sheet)

    table: list[list[str]] = []
    for row in sheet.iterfind("m:sheetData/m:row", _NS):
        number = int(row.get("r", len(table) + 1))
        while len(table) < number:
            table.append([])
        values = table[number - 1]
        for cell in row.iterfind("m:c", _NS):
            column = _column_index(cell.get("r", "")) if cell.get("r") else len(values)
            while len(values) <= column:
                values.append("")
            values[column] = _cell_text(cell, shared)
    return table, links


def _hyperlinks(archive: zipfile.ZipFile, sheet_path: str, sheet: ElementTree.Element) -> dict:
    """{(linha, coluna): destino} dos hyperlinks externos da aba (ex.: link para o .pfx)."""
    folder, _, name = sheet_path.rpartition("/")
    try:
        rels = ElementTree.fromstring(archive.read(f"{folder}/_rels/{name}.rels"))
    except KeyError:
        return {}
    targets = {rel.get("Id"): rel.get("Target", "") for rel in rels}
    links = {}
    for link in sheet.iterfind("m:hyperlinks/m:hyperlink", _NS):
        target = targets.get(link.get(f"{{{_REL_NS}}}id"))
        ref = re.match(r"([A-Z]+)(\d+)", link.get("ref", "").upper())
        if target and ref:
            links[(int(ref.group(2)), _column_index(ref.group(1)))] = target
    return links


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    first = workbook.find("m:sheets/m:sheet", _NS)
    if first is None:
        raise ConfigurationError("Planilha sem abas")
    rel_id = first.get(f"{{{_REL_NS}}}id")
    rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for rel in rels:
        if rel.get("Id") == rel_id:
            target = rel.get("Target", "").lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    raise ConfigurationError("Nao foi possivel localizar a primeira aba da planilha")


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")) for si in root.iterfind("m:si", _NS)]


def _column_index(reference: str) -> int:
    index = 0
    for char in re.match(r"[A-Z]+", reference.upper()).group(0):
        index = index * 26 + ord(char) - 64
    return index - 1


def _cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{{{_NS['m']}}}t"))
    value = cell.findtext("m:v", default="", namespaces=_NS)
    if kind == "s":
        return shared[int(value)] if value.isdigit() and int(value) < len(shared) else ""
    if kind == "n" and value:
        # 52998224725 pode vir como "52998224725", "52998224725.0" ou "5.2998224725E10"
        try:
            number = Decimal(value)
        except InvalidOperation:
            return value
        if number == number.to_integral_value():
            return str(int(number))
    return value
