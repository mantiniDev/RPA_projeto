# Runbook de homologação e operação

## 1. Definir o escopo

Registre o tribunal, a instância, a URL oficial e se o fluxo é primeiro cadastro ou confirmação
administrativa. Este projeto implementa **cadastro automatico operado pela pagina local**, com o A1 do titular.

## 2. Preparar sem dados reais

Execute:

```powershell
.\scripts\preparar.ps1
.\scripts\testar.ps1 -Python .\.venv\Scripts\python.exe
.\scripts\simular.ps1 -Python .\.venv\Scripts\python.exe
```

Também teste os cenários de parada:

```powershell
.\scripts\simular.ps1 -Scenario captcha -Python .\.venv\Scripts\python.exe
.\scripts\simular.ps1 -Scenario jus_postulandi -Python .\.venv\Scripts\python.exe
.\scripts\simular.ps1 -Scenario second_factor -Python .\.venv\Scripts\python.exe
.\scripts\simular.ps1 -Scenario already_registered -Python .\.venv\Scripts\python.exe
```

## 3. Levantar seletores do tribunal

Faça a inspeção em ambiente autorizado, usando somente a interface normal. Para cada grupo em
`[selectors]`, registre pelo menos um seletor semântico estável. Dê preferência a `role` e `label`;
use CSS apenas quando não houver alternativa. Não use coordenadas de tela.

Confirme especialmente os seletores da pagina do PJe e, alem deles, os nomes das janelas
do PJeOffice PRO (secao `[signer]`), levantados com:

```powershell
.\scripts\inspecionar-assinador.ps1
```

Confirme especialmente:

- botão de acesso por certificado;
- campos retornados de nome, CPF e OAB;
- formulário de contato e endereço;
- botão de confirmação;
- termo de compromisso e botão de assinatura;
- mensagem inequívoca de conclusão;
- campo e botao do segundo fator;
- painel de advogado já cadastrado;
- mensagens de CAPTCHA, bloqueio e erro de certificado;
- janela de selecao de certificado e de confirmacao do PJeOffice.

## 4. Homologar

Copie os arquivos de exemplo para `config.toml` e `cadastro.json`. Preencha a URL oficial e os
seletores. Mantenha inicialmente:

```toml
environment = "simulation"
selectors_approved = false
allow_live_submission = false
```

Depois da revisão por duas pessoas, altere para:

```toml
environment = "production"
selectors_approved = true
allow_live_submission = true
```

O modo real também bloqueia domínios que não terminem em `.jus.br` e dados ainda marcados como
exemplo/teste.

## 4.1 Ensaiar no PJe real

Antes de habilitar o envio, rode o ensaio. Ele exige apenas `environment = "production"` e
dominio `.jus.br`. Faz o login com o A1, confere nome/CPF/OAB, preenche o formulario e
**para antes de "Confirmar dados"**: nada e enviado nem assinado.

```powershell
.\scripts\ensaiar.ps1 -Tribunal tjpa
```

Cada tribunal tem seu arquivo em `tribunais\` (hoje: `tjap.toml`, `tjpa.toml`, `trt2.toml`). Para um novo
tribunal, copie um deles, troque `tribunal`/`base_url` e mapeie o botao do certificado.

Resultado esperado: `REHEARSAL_OK`. Se der `FAILED`, o evento `flow_failed` do `audit.jsonl`
traz em `page` os titulos, rotulos, botoes e nomes de campos da tela em que parou (nome do
titular e numeros mascarados). Use isso para ajustar os seletores e ensaie de novo.

## 5. Executar

Com o PJeOffice PRO aberto e validado:

```powershell
.\scripts\executar-live.ps1
```

Isso abre `http://127.0.0.1:8723/`. O robo conduz todo o cadastro. CAPTCHA ou divergencia
encerram a execucao.

### Dados pela planilha

A planilha (`.xlsx` ou `.csv`) e lida direto do disco. Os scripts usam
`%USERPROFILE%\cadastros-pje\advogados.xlsx` (ou `.csv`) se existir, ou o arquivo passado em
`-Planilha`:

```powershell
.\scripts\ensaiar.ps1 -Tribunal tjap -Planilha "C:\caminho\advogados.xlsx"
```

A pagina ja abre com a planilha carregada: escolha o advogado (se houver mais de um), confira
os campos e inicie. Depois de salvar mudancas no Excel, clique na faixa da planilha para
recarregar; ao iniciar, o robo sempre usa o que esta salvo no arquivo. Sem planilha fixa, ainda
da para arrastar uma planilha na pagina.

O modelo esta em `cadastro.modelo.csv`: uma linha por advogado, com as colunas Nome do
titular, CPF, OAB numero, OAB UF, E-mail, Telefone, CEP, Logradouro, Numero, Complemento,
Bairro, Cidade, UF, Referencia da autorizacao, Certificado, Senha do certificado e Chave 2FA.
Acentos e maiusculas nos nomes das colunas nao importam, e a OAB tambem pode vir numa coluna
so (`12345/BA`).

- **Certificado**: caminho do `.pfx`, como texto (`C:\Users\...\Downloads\certificado.pfx`) ou
  hyperlink do Excel. Com planilha fixa, o caminho pode ser relativo a pasta da planilha; com
  planilha arrastada na pagina, precisa ser completo.
- **Senha do certificado**: formate a coluna como **Texto** no Excel; senha so com numeros
  perde o zero da frente se a coluna for numerica.
- **Chave 2FA**: a chave secreta do app autenticador do advogado no PDPJ (o texto que aparece
  junto do QR code ao configurar o 2FA, ex.: `JBSW Y3DP EHPK 3PXP`, ou a URI `otpauth://`).
  Com ela o robo gera o codigo de 6 digitos na hora, como o app, e o operador nao precisa
  digitar nada. Nao coloque o codigo de 6 digitos: ele expira em 30 s. O relogio do Windows
  precisa estar certo (sincronizacao automatica ligada); se o codigo for recusado, o robo
  tenta o seguinte, ate 3 vezes. Sem chave, o codigo e pedido na pagina.
- Arrastar outro `.pfx` ou digitar a senha na pagina substitui o que veio da planilha.
- Senha e chave 2FA nunca voltam para a pagina nem vao para o log. A planilha guarda as duas
  em texto: veja `SECURITY.md` sobre onde guarda-la e quando apaga-la.

Para conferir a planilha antes, sem abrir o navegador (mostra se o certificado foi encontrado e
se senha e chave 2FA estao presentes, nunca os valores):

```powershell
.\.venv\Scripts\python.exe -m pje_rpa planilha "$env:USERPROFILE\cadastros-pje\advogados.xlsx"
```

## 6. Encerrar

Leia `runtime/audit/<run_id>/result.json` e verifique `audit.jsonl`:

```powershell
.\.venv\Scripts\python.exe -m pje_rpa verify-audit runtime\audit\<run_id>\audit.jsonl
```

Resultados possíveis:

- `COMPLETED`: tela final de conclusão encontrada;
- `ALREADY_REGISTERED`: painel do advogado já estava disponível;
- `NEEDS_HUMAN`: confirmação humana não concluída;
- `BLOCKED`: CAPTCHA, certificado, acesso ou perfil incorreto;
- `FAILED`: elemento esperado ou requisito técnico não encontrado.

