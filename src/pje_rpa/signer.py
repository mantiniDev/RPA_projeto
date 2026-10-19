"""Operacao das janelas do PJeOffice PRO instalado no computador.

O robo nao reimplementa nem imita o assinador: ele apenas opera a janela oficial
"Selecao de certificado", como um operador faria. O PJeOffice e um programa Java (Swing),
cujas janelas sao invisiveis para a automacao nativa do Windows; por isso a leitura e os
cliques passam pelo Java Access Bridge, que vem com o JRE do PJeOffice e precisa estar
ligado uma vez com `jabswitch.exe -enable` (seguido de reiniciar o PJeOffice).
"""
from __future__ import annotations

import ctypes
import os
import queue
import re
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .certificate import CertificateInfo

DEFAULT_JAB_DLL = r"C:\Program Files\PJeOffice Pro\jre\bin\WindowsAccessBridge-64.dll"


class SignerError(RuntimeError):
    pass


@dataclass(frozen=True)
class SignerConfig:
    window_title_regex: str = r"(?i)sele\w+o de certificado"
    appear_timeout_s: int = 45
    max_dialogs: int = 4
    jab_dll: str = DEFAULT_JAB_DLL
    # Aba da janela que lista o repositorio do Windows, onde o A1 e instalado temporariamente.
    store_option: str = "Windows"
    ok_button: str = "OK"


class SignerPort(ABC):
    @abstractmethod
    def authenticate(self, certificate: "CertificateInfo", password: str) -> None: ...

    @abstractmethod
    def sign(self, certificate: "CertificateInfo", password: str) -> None: ...


def choose_row(rows: list[list[str]], certificate: "CertificateInfo") -> int:
    """Escolhe a linha (Dispositivo, Nome, Emitido Por, Validade) do certificado em uso.

    O repositorio pode ter varios certificados com o mesmo nome (renovacoes, outra AC);
    por isso exige nome + emissor + validade e recusa qualquer ambiguidade.
    """
    name = _norm(certificate.nome)
    issuer = _norm(certificate.emissor)
    local_expiry = certificate.valido_ate.astimezone()
    stamp = local_expiry.strftime("%d/%m/%Y %H:%M:%S")
    day = local_expiry.strftime("%d/%m/%Y")
    same = [i for i, row in enumerate(rows) if len(row) >= 4 and _norm(row[1]) == name and _norm(row[2]) == issuer]
    exact = [i for i in same if rows[i][3].strip() == stamp]
    by_day = [i for i in same if rows[i][3].strip().startswith(day)]
    for candidates in (exact, by_day):
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise SignerError("Mais de um certificado identico no PJeOffice; remova duplicatas do repositorio")
    raise SignerError(
        f"Certificado de {certificate.safe_summary()['titular']} ({certificate.emissor}, validade {day}) "
        "nao aparece na lista do PJeOffice"
    )


def _norm(value: str) -> str:
    return " ".join(value.upper().split())


class PJeOfficeSigner(SignerPort):
    def __init__(self, config: SignerConfig) -> None:
        if not os.path.isfile(config.jab_dll):
            raise SignerError(f"Java Access Bridge nao encontrado em {config.jab_dll}")
        os.environ["RC_JAVA_ACCESS_BRIDGE_DLL"] = config.jab_dll
        _import_jab_quietly()
        self.config = config
        self._jab = _start_jab()

    def authenticate(self, certificate: "CertificateInfo", password: str) -> None:
        self._handle_dialogs(certificate, purpose="autenticacao")

    def sign(self, certificate: "CertificateInfo", password: str) -> None:
        self._handle_dialogs(certificate, purpose="assinatura")

    def _handle_dialogs(self, certificate: "CertificateInfo", purpose: str) -> None:
        deadline = time.monotonic() + self.config.appear_timeout_s
        handled = 0
        while time.monotonic() < deadline and handled < self.config.max_dialogs:
            window = self._find_window()
            if window is None:
                if handled:
                    return  # janela tratada e fechada pelo PJeOffice
                time.sleep(0.5)
                continue
            self._select_and_confirm(window, certificate, purpose)
            handled += 1
            # Espera a janela fechar antes de procurar uma eventual nova (ex.: outra etapa).
            close_deadline = time.monotonic() + 10
            while time.monotonic() < close_deadline and ctypes.windll.user32.IsWindowVisible(window.hwnd):
                time.sleep(0.3)
            time.sleep(1.0)
        if not handled:
            raise SignerError(
                f"Janela 'Selecao de certificado' do PJeOffice ({purpose}) nao apareceu em "
                f"{self.config.appear_timeout_s}s. Confira se o PJeOffice esta aberto e a tela desbloqueada."
            )

    def _find_window(self):
        for window in self._jab.get_windows():
            if ctypes.windll.user32.IsWindowVisible(window.hwnd) and re.search(
                self.config.window_title_regex, window.title or ""
            ):
                return window
        return None

    def _tree(self, window):
        from JABWrapper.context_tree import ContextTree

        vm_id, context = self._jab.get_accessible_context_from_hwnd(window.hwnd)
        self._jab.set_hwnd(window.hwnd)
        self._jab.set_context(vm_id, context)
        return list(ContextTree(self._jab, max_depth=30))

    def _select_and_confirm(self, window, certificate: "CertificateInfo", purpose: str) -> None:
        nodes = self._tree(window)
        store = _find(nodes, "radio button", self.config.store_option)
        if store is not None and not _has_state(self._jab, store, "selecionado", "selected"):
            _press(store)
            time.sleep(2.0)  # o PJeOffice recarrega a lista
            nodes = self._tree(window)

        # Nunca memoriza o certificado como padrao: a proxima execucao pode ser de outro titular.
        for node in nodes:
            ci = node.context_info
            if ci.role == "check box" and "memorizar" in ci.name.lower() and _has_state(
                self._jab, node, "marcado", "checked"
            ):
                _press(node)

        table = next((n for n in nodes if n.context_info.role == "table"), None)
        if table is None:
            raise SignerError(f"Lista de certificados nao encontrada na janela do PJeOffice ({purpose})")
        row = choose_row(self._read_table(table), certificate)
        info = self._jab.get_accessible_table_info(table.context)
        self._jab.clear_accessible_selection_from_context(table.context)
        self._jab.add_accessible_selection_from_context(
            table.context, self._jab.get_accessible_table_index(table.context, row, min(1, info.columnCount - 1))
        )
        time.sleep(0.5)
        if not self._jab.is_accessible_table_row_selected(table.context, row):
            raise SignerError("Nao foi possivel selecionar o certificado na lista do PJeOffice")

        ok = _find(nodes, "push button", self.config.ok_button)
        if ok is None or not _has_state(self._jab, ok, "ativado", "enabled"):
            raise SignerError(f"Botao {self.config.ok_button} do PJeOffice indisponivel ({purpose})")
        _press(ok)

    def _read_table(self, table) -> list[list[str]]:
        info = self._jab.get_accessible_table_info(table.context)
        rows = []
        for r in range(info.rowCount):
            cells = []
            for c in range(info.columnCount):
                cell = self._jab.get_accessible_table_cell_info(table.context, r, c)
                cells.append(self._jab.get_context_info(cell.accessibleContext).name or "")
            rows.append(cells)
        return rows


def _import_jab_quietly() -> None:
    """Importa o JABWrapper sem o log que ele cria sozinho.

    Ao ser importado, o JABWrapper grava `jab_wrapper.log` em DEBUG (com tudo o que le, inclusive
    os nomes da lista de certificados) e liga o logging raiz em DEBUG. Aqui o arquivo vai para uma
    pasta temporaria, e removida em seguida, e o logging volta ao nivel normal.
    """
    import logging
    import shutil
    import tempfile

    if "JABWrapper.jab_wrapper" in sys.modules:
        return
    scratch = tempfile.mkdtemp(prefix="pje-rpa-jab-")
    previous = os.environ.get("ROBOT_ARTIFACTS")
    os.environ["ROBOT_ARTIFACTS"] = scratch
    try:
        import JABWrapper.jab_wrapper  # noqa: F401
    except ImportError as exc:
        raise SignerError("java-access-bridge-wrapper nao instalado. Execute scripts\\preparar.ps1") from exc
    finally:
        if previous is None:
            os.environ.pop("ROBOT_ARTIFACTS", None)
        else:
            os.environ["ROBOT_ARTIFACTS"] = previous
        root = logging.getLogger()
        for handler in list(root.handlers):
            if isinstance(handler, logging.FileHandler) and handler.baseFilename.startswith(os.path.abspath(scratch)):
                root.removeHandler(handler)
                handler.close()
        root.setLevel(logging.WARNING)
        shutil.rmtree(scratch, ignore_errors=True)


def _start_jab():
    """O Access Bridge precisa de uma fila de mensagens do Windows rodando em segundo plano."""
    from ctypes import byref, wintypes

    from JABWrapper.jab_wrapper import JavaAccessBridgeWrapper

    pipe: "queue.Queue" = queue.Queue()

    def pump() -> None:
        try:
            jab = JavaAccessBridgeWrapper()
        except Exception as exc:  # noqa: BLE001
            pipe.put(exc)
            return
        pipe.put(jab)
        message = byref(wintypes.MSG())
        while ctypes.windll.user32.GetMessageW(message, 0, 0, 0) > 0:
            ctypes.windll.user32.TranslateMessage(message)
            ctypes.windll.user32.DispatchMessageW(message)

    threading.Thread(target=pump, name="java-access-bridge", daemon=True).start()
    result = pipe.get(timeout=15)
    if isinstance(result, Exception):
        raise SignerError(f"Falha ao iniciar o Java Access Bridge: {result}")
    time.sleep(0.5)  # deixa o bridge processar as mensagens iniciais
    return result


def _find(nodes, role: str, name: str):
    target = name.strip().casefold()
    return next(
        (n for n in nodes if n.context_info.role == role and (n.context_info.name or "").strip().casefold() == target),
        None,
    )


def _has_state(jab, node, *states: str) -> bool:
    current = (jab.get_context_info(node.context).states or "").casefold()
    return any(state in current.split(",") for state in states)


def _press(node) -> None:
    # Os nomes das acoes vem traduzidos pelo Java ("clicar" no PJeOffice em portugues).
    actions = node.get_actions()
    action = next((a for a in actions if a in {"clicar", "click"}), actions[0] if actions else None)
    if action is None:
        raise SignerError(f"Elemento '{node.context_info.name}' do PJeOffice nao aceita clique")
    node.do_action(action)


def dump_signer_windows(config: SignerConfig) -> str:
    """Lista a arvore das janelas Java visiveis do PJeOffice (para homologacao)."""
    signer = PJeOfficeSigner(config)
    lines = []
    for window in signer._jab.get_windows():
        if not ctypes.windll.user32.IsWindowVisible(window.hwnd) or not window.title:
            continue
        lines.append(f"=== Janela: {window.title!r}")
        for node in signer._tree(window):
            ci = node.context_info
            if ci.role == "label" and node.parent is not None and node.parent.context_info.role == "table":
                continue  # nao lista os certificados de terceiros
            lines.append(f"{'  ' * node.ancestry}{ci.role} | {ci.name!r} | {ci.states}")
    return "\n".join(lines) or "Nenhuma janela Java visivel. Abra a Selecao de certificado e rode novamente."


class SimulatedSigner(SignerPort):
    def __init__(self, browser) -> None:
        self.browser = browser
        self.calls: list[str] = []

    def authenticate(self, certificate: "CertificateInfo", password: str) -> None:
        self.calls.append("authenticate")
        self.browser.human_completed_certificate()

    def sign(self, certificate: "CertificateInfo", password: str) -> None:
        self.calls.append("sign")
