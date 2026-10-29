# PJe Advogado RPA

Cadastro **automatico** do advogado no PJe usando o certificado A1 e o **PJeOffice PRO
oficial** ja instalado no computador. Voce abre uma pagina local, solta o arquivo `.pfx`,
digita a senha e confirma os dados; o robo conduz login, preenchimento, termo de
compromisso e conclusao. A assinatura criptografica continua sendo feita pelo PJeOffice
oficial — o projeto nao reimplementa o assinador.

## Como funciona

1. A pagina local (`127.0.0.1`) recebe o `.pfx` e a senha **em memoria** — nunca em disco.
2. O certificado e importado temporariamente no repositorio do Windows (chave **nao
   exportavel**) para o PJeOffice PRO encontra-lo.
3. O Playwright conduz o navegador; o Java Access Bridge opera a janela "Selecao de certificado" do PJeOffice
   (selecao do certificado e confirmacao de assinatura).
4. Ao final, o certificado e **removido** do repositorio, com a chave privada.

## Paradas que continuam existindo

Estas nao sao contornaveis e sao intencionais:

- **CAPTCHA** ou bloqueio de acesso: a execucao encerra sem tentativa de contorno.
- **Segundo fator** (PJe SSO): o codigo chega no e-mail/celular do advogado; a pagina
  pede esse codigo e segue.
- **Divergencia** de nome ou CPF entre certificado, dados informados e o que o PJe
  devolve: encerra antes de qualquer envio.

## Inicio rapido (PowerShell, nesta pasta)

```powershell
.\scripts\preparar.ps1
.\scripts\testar.ps1    -Python .\.venv\Scripts\python.exe
.\scripts\simular.ps1   -Python .\.venv\Scripts\python.exe
```

## Configuracao real

1. `scripts\preparar.ps1` (instala dependencias, Playwright/Edge e copia `config.toml`).
2. Edite `config.toml`: `tribunal`, `base_url` oficial, seletores homologados e a secao
   `[signer]`.
3. Com o PJeOffice aberto na tela de certificado, ligue o Java Access Bridge
   (`& "C:\Program Files\PJeOffice Pro\jrein\jabswitch.exe" -enable`), reinicie o PJeOffice e,
   com a janela "Selecao de certificado" aberta, confira-a com `scripts\inspecionar-assinador.ps1`.
4. Homologue tudo em simulacao (veja `RUNBOOK.md`).
5. Habilite as tres chaves do modo real:
   ```toml
   environment = "production"
   selectors_approved = true
   allow_live_submission = true
   ```
6. `scripts\executar-live.ps1` abre a pagina local. Deixe o PJeOffice PRO rodando antes.
   Os dados do advogado podem ser digitados ou vir de uma planilha `.xlsx`/`.csv` no formato de
   `cadastro.modelo.csv`, que tambem pode trazer o caminho do `.pfx` e a senha. Veja o
   `RUNBOOK.md`, secao 5, e o `SECURITY.md`.

## Limites importantes

- Um A1 identifica seu titular: o cadastro so prossegue se nome e CPF do certificado
  baterem com os dados informados. Nao ha cadastro em massa de terceiros neste projeto.
- O robo nunca grava a senha; ela vive em memoria durante a execucao. Se vier de planilha,
  a planilha e um arquivo com a senha em texto e fica sob cuidado do operador (`SECURITY.md`).
- O modo real exige dominio oficial terminado em `.jus.br` e a referencia da autorizacao
  do advogado (`autorizacao_ref`).
- O robo nao usa endpoints internos, engenharia reversa ou chamadas privadas do PJe.

## Base legal da assinatura

O cadastro termina na assinatura do **termo de compromisso**, declaracao pessoal do
advogado. Com A1, a assinatura ICP-Brasil presume autoria do titular (MP 2.200-2/2001).
Rodar isso sem o titular presente so se sustenta se houver autorizacao documentada do
advogado para a operacao — registre essa referencia em `autorizacao_ref` e valide o
modelo com o juridico antes de producao.

## Arquivos principais

- `src/pje_rpa/webapp.py`: pagina local e orquestracao da execucao.
- `src/pje_rpa/certificate.py`: leitura do A1 e instalacao/remocao no repositorio Windows.
- `src/pje_rpa/signer.py`: operacao das janelas do PJeOffice PRO.
- `src/pje_rpa/flow.py`: maquina de estados do cadastro.
- `src/pje_rpa/browser.py`: adaptadores Playwright e simulado.
- `src/pje_rpa/audit.py`: log encadeado por SHA-256.
- `SECURITY.md`, `RUNBOOK.md`: controles e operacao.

## Fontes oficiais

- Manual do Advogado: https://docs.pje.jus.br/manuais-de-uso/Manual%20do%20advogado/
- PJeOffice Pro: https://docs.pje.jus.br/servicos-negociais/pjeoffice-pro/
- Duplo fator no PJe SSO: https://docs.pje.jus.br/servicos-negociais/duplo-fator-autenticacao-pje-sso/
