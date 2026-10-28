"""Pagina local onde o operador solta o .pfx + senha e o cadastro roda sozinho.

Escuta SOMENTE em 127.0.0.1. O .pfx e a senha nunca sao gravados: ficam em memoria
durante a execucao e sao descartados ao final. O certificado e instalado
temporariamente no repositorio do Windows para o PJeOffice PRO oficial assinar,
e removido (com a chave privada) ao terminar.
"""
from __future__ import annotations

import json
import queue
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .audit import AuditLog
from .browser import PlaywrightBrowser
from .certificate import CertificateError, WindowsCertificateStore, read_pfx
from .config import (
    ConfigurationError,
    assert_live_ready,
    assert_rehearsal_ready,
    lawyer_from_dict,
    load_config,
    validate_lawyer,
)
from .flow import PJeRegistrationFlow
from .models import LawyerRecord, digits
from .planilha import read_sheet
from .signer import PJeOfficeSigner


# .pfx ou planilha (ate 5 MB) em base64, mais o JSON em volta.
_MAX_BODY = 8 * 1024 * 1024


class _Session:
    """Guarda o estado de uma execucao e a fila de eventos para a pagina (SSE)."""

    def __init__(self) -> None:
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.code_box: "queue.Queue[str | None]" = queue.Queue(maxsize=1)
        self.running = False

    def emit(self, kind: str, **data) -> None:
        self.events.put({"kind": kind, **data})


class RegistrationServer:
    def __init__(
        self,
        config_path: Path,
        audit_root: Path,
        host: str = "127.0.0.1",
        port: int = 8723,
        rehearsal: bool = False,
    ) -> None:
        self.config = load_config(config_path)
        self.audit_root = audit_root
        self.host = host
        self.port = port
        self.rehearsal = rehearsal
        self.session = _Session()
        # Hosts/origens aceitos: bloqueia sites abertos no mesmo navegador e DNS rebinding.
        self._allowed_hosts = {f"{name}:{port}" for name in ("127.0.0.1", "localhost", "[::1]")}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # silencia o log padrao
                pass

            def do_GET(self):
                if not self._trusted(require_origin=False):
                    return
                if self.path == "/":
                    page = _PAGE.replace("__MODE__", "ensaio" if server.rehearsal else "real")
                    self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
                elif self.path == "/events":
                    self._stream()
                else:
                    self._send(404, "text/plain", b"nao encontrado")

            def do_POST(self):
                # Le o corpo antes de responder: recusar sem ler faz o Windows abortar a conexao.
                length = int(self.headers.get("Content-Length", 0))
                if length > _MAX_BODY:
                    self.close_connection = True
                    self._send(413, "text/plain", b"arquivo grande demais")
                    return
                body = self.rfile.read(length)
                if not self._trusted(require_origin=True):
                    return
                if self.path == "/start":
                    self._start(body)
                elif self.path == "/planilha":
                    status, answer = _sheet_answer(body)
                    self._send(status, "application/json", json.dumps(answer, ensure_ascii=False).encode("utf-8"))
                elif self.path == "/code":
                    try:
                        server.session.code_box.put_nowait((json.loads(body).get("code") or "").strip() or None)
                    except queue.Full:
                        self._send(409, "application/json", b'{"error":"codigo ja enviado"}')
                        return
                    self._send(200, "application/json", b'{"ok":true}')
                else:
                    self._send(404, "text/plain", b"nao encontrado")

            # ---- helpers
            def _trusted(self, require_origin: bool) -> bool:
                host_ok = self.headers.get("Host", "") in server._allowed_hosts
                origin = self.headers.get("Origin")
                origin_ok = origin in {f"http://{h}" for h in server._allowed_hosts} if origin else not require_origin
                if host_ok and origin_ok:
                    return True
                self._send(403, "text/plain", b"origem nao permitida")
                return False

            def _send(self, status, ctype, payload):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                while True:
                    event = server.session.events.get()
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    if event["kind"] in {"done", "error"}:
                        break

            def _start(self, body):
                if server.session.running:
                    self._send(409, "application/json", b'{"error":"ja em execucao"}')
                    return
                try:
                    payload = json.loads(body)
                    pfx = _decode_pfx(payload["pfx_base64"])
                    password = payload["password"]
                    record = _record_from_payload(payload["cadastro"])
                except (KeyError, ValueError) as exc:
                    self._send(400, "application/json",
                               json.dumps({"error": f"entrada invalida: {exc}"}).encode("utf-8"))
                    return
                server.session = _Session()
                server.session.running = True
                threading.Thread(target=server._execute, args=(pfx, password, record), daemon=True).start()
                self._send(200, "application/json", b'{"ok":true}')

        self._httpd = ThreadingHTTPServer((host, port), Handler)

    def serve_forever(self) -> None:
        self._cleanup_pending("execucao anterior interrompida")
        mode = "ENSAIO (para antes de enviar)" if self.rehearsal else "REAL"
        print(f"Pagina local de cadastro em http://{self.host}:{self.port}/  modo {mode}  (Ctrl+C encerra)")
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            self._httpd.server_close()
            self._cleanup_pending("encerramento com Ctrl+C")

    # ------------------------------------------------------- certificados pendentes
    # O thumbprint (publico, sem segredo) e anotado ANTES da instalacao e apagado depois da
    # remocao. Se o processo morrer no meio (Ctrl+C, janela fechada, queda), a proxima
    # abertura da pagina remove o que ficou no repositorio do Windows.
    @property
    def _pending_path(self) -> Path:
        return self.audit_root.parent / "certificados-pendentes.txt"

    def _pending(self) -> list[str]:
        try:
            return [t for t in self._pending_path.read_text(encoding="ascii").split() if len(t) == 40]
        except FileNotFoundError:
            return []

    def _set_pending(self, thumbprints: list[str]) -> None:
        if thumbprints:
            self._pending_path.parent.mkdir(parents=True, exist_ok=True)
            self._pending_path.write_text("\n".join(sorted(set(thumbprints))) + "\n", encoding="ascii")
        else:
            self._pending_path.unlink(missing_ok=True)

    def _cleanup_pending(self, why: str) -> None:
        remaining = []
        for thumbprint in self._pending():
            try:
                WindowsCertificateStore().remove(thumbprint)
                print(f"Certificado {thumbprint[:8]}... removido do repositorio do Windows ({why})")
            except Exception:  # noqa: BLE001
                remaining.append(thumbprint)
                print(f"ATENCAO: nao foi possivel remover o certificado {thumbprint[:8]}...; remova pelo certmgr.msc")
        self._set_pending(remaining)

    # ------------------------------------------------------------------ execucao
    def _execute(self, pfx: bytes, password: str, record: LawyerRecord) -> None:
        session = self.session
        store = WindowsCertificateStore()
        thumbprint = None
        browser = None
        try:
            session.emit("step", text="Lendo o certificado A1")
            info = read_pfx(pfx, password)
            errors = validate_lawyer(record)
            if errors:
                raise ConfigurationError("; ".join(errors))
            if self.rehearsal:
                assert_rehearsal_ready(self.config, record)
            else:
                assert_live_ready(self.config, record)

            if store.contains(info.thumbprint_sha1):
                # Ja estava instalado antes do robo: usa e NAO remove ao final (nao e nosso).
                session.emit("step", text="Certificado ja estava no repositorio do Windows; sera mantido")
            else:
                session.emit("step", text=f"Instalando o certificado de {info.safe_summary()['titular']} no repositorio")
                self._set_pending(self._pending() + [info.thumbprint_sha1])
                thumbprint = info.thumbprint_sha1
                installed = store.install(pfx, password)
                if installed != thumbprint:
                    self._set_pending(self._pending() + [installed])
                    thumbprint = installed

            audit = AuditLog(self.audit_root)
            audit.on_record = lambda event, state: session.emit("log", event=event, state=state)
            session.emit("run", run_id=audit.run_id)

            signer = PJeOfficeSigner(self.config.signer)
            browser = PlaywrightBrowser(self.config)
            flow = PJeRegistrationFlow(
                self.config, record, info, password, browser, signer, audit,
                code_provider=lambda msg: self._ask_code(session, msg),
                rehearsal=self.rehearsal,
            )
            result = flow.run()
            serialized = asdict(result)
            serialized["status"] = result.status.value
            serialized["state"] = result.state.value
            audit.write_result(serialized)
            session.emit("done", status=result.status.value, reason=result.reason, run_id=audit.run_id)
        except (CertificateError, ConfigurationError) as exc:
            session.emit("error", reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - a pagina mostra o tipo, o log tem o resto
            session.emit("error", reason=f"Falha inesperada: {type(exc).__name__}")
        finally:
            password = ""
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if thumbprint:
                try:
                    store.remove(thumbprint)
                    self._set_pending([t for t in self._pending() if t != thumbprint])
                    session.emit("step", text="Certificado removido do repositorio")
                except Exception:
                    session.emit("step", text="ATENCAO: remova o certificado manualmente do repositorio do Windows")
            session.running = False

    def _ask_code(self, session: _Session, message: str) -> "str | None":
        session.emit("code_request", text=message)
        try:
            return session.code_box.get(timeout=self.config.signer.appear_timeout_s * 4 if self.config.signer else 180)
        except queue.Empty:
            return None


def _decode_pfx(value: str) -> bytes:
    import base64

    return base64.b64decode(value)


def _record_from_payload(raw: dict) -> LawyerRecord:
    return lawyer_from_dict(raw)


def _sheet_answer(body: bytes) -> "tuple[int, dict]":
    """Le a planilha enviada pela pagina (so em memoria) e devolve as linhas para preencher."""
    try:
        payload = json.loads(body)
        rows = read_sheet(str(payload["nome"]), _decode_pfx(payload["base64"]))
    except ConfigurationError as exc:
        return 400, {"error": str(exc)}
    except (KeyError, ValueError, TypeError):
        return 400, {"error": "planilha invalida"}
    return 200, {"linhas": [asdict(row) for row in rows]}


_PAGE = """<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cadastro de advogado no PJe</title>
<style>
  :root{color-scheme:light dark;--bg:#0f1115;--panel:#171a21;--ink:#e7e9ee;--muted:#9aa3b2;
        --line:#272b35;--accent:#5b8def;--ok:#3ecf8e;--warn:#e0a33e;--err:#e5484d;}
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.5 "Inter",system-ui,sans-serif;background:var(--bg);color:var(--ink);
       display:grid;place-items:start center;padding:32px 16px}
  main{width:100%;max-width:560px}
  h1{font-size:1.35rem;font-weight:650;margin:0 0 4px}
  p.sub{color:var(--muted);margin:0 0 24px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px}
  label{display:block;font-weight:550;margin:12px 0 4px}
  input,select{width:100%;padding:10px 12px;background:#0d0f14;border:1px solid var(--line);
        border-radius:8px;color:var(--ink);font:inherit}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .drop{border:1.5px dashed var(--line);border-radius:10px;padding:22px;text-align:center;color:var(--muted);cursor:pointer}
  .drop.has{border-color:var(--accent);color:var(--ink)}
  #sheetdrop{padding:14px;margin-bottom:4px}
  #sheetnote:empty{display:none}
  .note.err{color:var(--err)}
  button{margin-top:20px;width:100%;padding:12px;border:0;border-radius:9px;background:var(--accent);
         color:#fff;font:inherit;font-weight:600;cursor:pointer}
  button:disabled{opacity:.5;cursor:not-allowed}
  #steps{list-style:none;padding:0;margin:0}
  #steps li{padding:8px 0;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:baseline}
  #steps li:last-child{border:0}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none;translate:0 4px}
  .dot.ok{background:var(--ok)} .dot.err{background:var(--err)} .dot.warn{background:var(--warn)}
  .hidden{display:none}
  .note{font-size:.82rem;color:var(--muted);margin-top:8px}
  .verdict{font-weight:650;padding:12px;border-radius:9px;margin-top:4px}
  .verdict.ok{background:rgba(62,207,142,.12);color:var(--ok)}
  .verdict.err{background:rgba(229,72,77,.12);color:var(--err)}
  .mode{font-weight:600;padding:10px 12px;border-radius:9px;margin:0 0 16px}
  .mode.ensaio{background:rgba(224,163,62,.14);color:var(--warn)}
  .mode.real{background:rgba(229,72,77,.14);color:var(--err)}
</style></head><body><main>
  <h1>Cadastro de advogado no PJe</h1>
  <p class="sub">O certificado e a senha ficam neste computador e sao apagados ao final.</p>
  <p class="mode" id="mode"></p>

  <div class="card" id="form">
    <div class="drop" id="sheetdrop">Opcional: arraste a planilha com os dados (.xlsx / .csv) para preencher os campos
      <input type="file" id="sheet" accept=".xlsx,.csv" class="hidden"></div>
    <div id="sheetpick" class="hidden"><label>Advogado da planilha</label><select id="sheetrow"></select></div>
    <p class="note" id="sheetnote"></p>
    <div class="drop" id="drop">Arraste o arquivo .pfx / .p12 aqui ou clique para escolher
      <input type="file" id="pfx" accept=".pfx,.p12" class="hidden"></div>
    <label>Senha do certificado</label>
    <input type="password" id="password" autocomplete="off">
    <div class="row">
      <div><label>Nome do titular</label><input id="nome"></div>
      <div><label>CPF</label><input id="cpf" inputmode="numeric"></div>
    </div>
    <div class="row">
      <div><label>OAB numero</label><input id="oab_numero" inputmode="numeric"></div>
      <div><label>OAB UF</label><input id="oab_uf" maxlength="2"></div>
    </div>
    <div class="row">
      <div><label>E-mail</label><input id="email"></div>
      <div><label>Telefone</label><input id="telefone" inputmode="numeric"></div>
    </div>
    <div class="row">
      <div><label>CEP</label><input id="cep" inputmode="numeric"></div>
      <div><label>Numero</label><input id="numero"></div>
    </div>
    <label>Logradouro</label><input id="logradouro">
    <div class="row">
      <div><label>Bairro</label><input id="bairro"></div>
      <div><label>Complemento</label><input id="complemento"></div>
    </div>
    <div class="row">
      <div><label>Cidade</label><input id="cidade"></div>
      <div><label>UF</label><input id="uf" maxlength="2"></div>
    </div>
    <label>Referencia da autorizacao do advogado</label><input id="autorizacao_ref"
      placeholder="contrato/termo que autoriza o uso do A1 (minimo 3 caracteres)">
    <p class="note">O cadastro so segue se nome e CPF do certificado baterem com estes dados.</p>
    <button id="go" disabled>Iniciar cadastro</button>
  </div>

  <div class="card hidden" id="progress">
    <ul id="steps"></ul>
    <div id="codebox" class="hidden">
      <label>Codigo de segundo fator</label>
      <input id="code" inputmode="numeric" autocomplete="off">
      <button id="sendcode">Enviar codigo</button>
    </div>
    <div id="verdict" class="hidden"></div>
  </div>
<script>
const $=id=>document.getElementById(id);
const MODE="__MODE__";
$("mode").className="mode "+MODE;
$("mode").textContent=MODE==="ensaio"
  ?"Modo ENSAIO: entra no PJe e confere os dados, mas para antes de enviar. Nada e assinado."
  :"Modo REAL: o cadastro sera enviado e o termo de compromisso assinado com este certificado.";
$("go").textContent=MODE==="ensaio"?"Iniciar ensaio":"Iniciar cadastro";
let pfxB64=null;
const drop=$("drop"),file=$("pfx");
drop.onclick=()=>file.click();
drop.ondragover=e=>{e.preventDefault();drop.classList.add("has")};
drop.ondragleave=()=>drop.classList.remove("has");
drop.ondrop=e=>{e.preventDefault();file.files=e.dataTransfer.files;load()};
file.onchange=load;
function load(){const f=file.files[0];if(!f)return;const r=new FileReader();
  r.onload=()=>{pfxB64=r.result.split(",")[1];drop.textContent=f.name;drop.classList.add("has");check()};
  r.readAsDataURL(f);}
// Planilha: lida no servidor local so em memoria; aqui so preenche os campos (que continuam editaveis).
let sheetRows=[];
const sheetdrop=$("sheetdrop"),sheet=$("sheet");
sheetdrop.onclick=()=>sheet.click();
sheetdrop.ondragover=e=>{e.preventDefault();sheetdrop.classList.add("has")};
sheetdrop.ondragleave=()=>sheetdrop.classList.remove("has");
sheetdrop.ondrop=e=>{e.preventDefault();sheet.files=e.dataTransfer.files;loadSheet()};
sheet.onchange=loadSheet;
function sheetNote(text,err){$("sheetnote").textContent=text;$("sheetnote").className="note"+(err?" err":"");}
function loadSheet(){const f=sheet.files[0];if(!f)return;const r=new FileReader();
  r.onload=async()=>{
    const resp=await fetch("/planilha",{method:"POST",body:JSON.stringify({nome:f.name,base64:r.result.split(",")[1]})});
    const ans=await resp.json();
    if(!resp.ok){sheetNote(ans.error,true);$("sheetpick").classList.add("hidden");return;}
    sheetRows=ans.linhas;sheetdrop.textContent=f.name;sheetdrop.classList.add("has");
    const sel=$("sheetrow");sel.innerHTML="";
    sheetRows.forEach((row,i)=>sel.add(new Option(`Linha ${row.linha} - ${row.cadastro.nome_esperado||"(sem nome)"}`
      +(row.erros.length?" - com erros":""),i)));
    $("sheetpick").classList.toggle("hidden",sheetRows.length<2);
    fillFromSheet(0);};
  r.readAsDataURL(f);}
$("sheetrow").onchange=e=>fillFromSheet(+e.target.value);
function fillFromSheet(i){const row=sheetRows[i];if(!row)return;const c=row.cadastro,e=c.endereco||{};
  const map={nome:c.nome_esperado,cpf:c.cpf_esperado,oab_numero:c.oab_numero,oab_uf:c.oab_uf,email:c.email,
    telefone:c.telefone,autorizacao_ref:c.autorizacao_ref,cep:e.cep,logradouro:e.logradouro,numero:e.numero,
    complemento:e.complemento,bairro:e.bairro,cidade:e.cidade,uf:e.uf};
  Object.entries(map).forEach(([id,v])=>$(id).value=v||"");
  sheetNote(row.erros.length?`Linha ${row.linha}: corrija antes de iniciar - ${row.erros.join("; ")}`
    :`Linha ${row.linha} carregada. Confira os campos e digite a senha do certificado.`,row.erros.length>0);
  check();}
const req=["password","nome","cpf","oab_numero","oab_uf","email","telefone","cep","numero","logradouro","bairro","cidade","uf","autorizacao_ref"];
req.forEach(id=>$(id).oninput=check);
function check(){$("go").disabled=!(pfxB64&&req.every(id=>$(id).value.trim())
  &&$("autorizacao_ref").value.trim().length>=3)}
function addStep(text,cls){const li=document.createElement("li");
  li.innerHTML=`<span class="dot ${cls||''}"></span><span>${text}</span>`;$("steps").append(li);}
$("go").onclick=async()=>{
  $("form").classList.add("hidden");$("progress").classList.remove("hidden");
  const cadastro={nome_esperado:$("nome").value,cpf_esperado:$("cpf").value,
    oab_numero:$("oab_numero").value,oab_uf:$("oab_uf").value,email:$("email").value,
    telefone:$("telefone").value,autorizacao_ref:$("autorizacao_ref").value,
    endereco:{cep:$("cep").value,logradouro:$("logradouro").value,numero:$("numero").value,
      complemento:$("complemento").value,bairro:$("bairro").value,cidade:$("cidade").value,uf:$("uf").value}};
  const es=new EventSource("/events");
  es.onmessage=ev=>{const m=JSON.parse(ev.data);
    if(m.kind==="step")addStep(m.text);
    else if(m.kind==="log")addStep(m.event.replace(/_/g," "),"ok");
    else if(m.kind==="code_request"){addStep(m.text,"warn");$("codebox").classList.remove("hidden");}
    else if(m.kind==="done"){const ok=["COMPLETED","ALREADY_REGISTERED","REHEARSAL_OK"].includes(m.status);
      show(ok,m.status+" — "+m.reason);es.close();}
    else if(m.kind==="error"){show(false,m.reason);es.close();}};
  await fetch("/start",{method:"POST",body:JSON.stringify({pfx_base64:pfxB64,password:$("password").value,cadastro})});
};
$("sendcode").onclick=async()=>{await fetch("/code",{method:"POST",body:JSON.stringify({code:$("code").value})});
  $("codebox").classList.add("hidden");};
function show(ok,text){const v=$("verdict");v.textContent=text;v.className="verdict "+(ok?"ok":"err");
  v.classList.remove("hidden");}
</script></main></body></html>"""
