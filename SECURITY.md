# Seguranca operacional

## Como o A1 e tratado

- A pagina local escuta **somente em 127.0.0.1**. O `.pfx` e a senha trafegam apenas
  entre o navegador e o processo local, em memoria.
- A senha e enviada ao PowerShell/PJeOffice **pelo stdin**, nunca como argumento de
  linha de comando (que apareceria na lista de processos).
- O `.pfx` **nao e gravado em disco** pelo robo. O certificado e importado no repositorio
  `CurrentUser\My` como chave **nao exportavel** e **removido ao final**, com a chave
  privada.
- A assinatura e feita pelo **PJeOffice PRO oficial**. O robo nao implementa nem imita o
  assinador; apenas opera as janelas oficiais.

## O que este projeto deliberadamente nao faz

- Nao automatiza CAPTCHA.
- Nao busca o codigo de segundo fator sozinho — ele e informado na pagina.
- Nao prossegue quando nome ou CPF divergem entre certificado, dados e PJe.
- Nao tenta cadastrar pessoa diferente do titular do certificado.
- Nao usa endpoints internos nem engenharia reversa do PJe.

## Preparacao do computador

1. Conta Windows individual e protegida, de uso exclusivo do operador autorizado.
2. PJeOffice PRO instalado a partir da fonte oficial e validado uma vez manualmente.
3. Nao deixe o `.pfx` dentro desta pasta; use-o apenas pela pagina.
4. Restrinja o acesso a pasta `runtime/`.
5. Use somente a URL oficial do tribunal (`.jus.br`).

## Autorizacao

Rodar o cadastro sem o titular presente exige autorizacao documentada do advogado para
o uso do A1 e a assinatura do termo de compromisso. A referencia desse documento
(`autorizacao_ref`) e registrada no log de auditoria de cada execucao.

## Evidencias e logs

Log em JSON Lines encadeado por SHA-256; adulteracao e detectada por `verify-audit`.
CPF, e-mail, telefone, endereco e segredos sao removidos antes da gravacao. A senha
nunca entra no log. Capturas de tela ficam desabilitadas.

## Incidentes

Em suspeita de acesso indevido a chave privada ou a senha, interrompa o RPA, isole o
computador e acione imediatamente a revogacao junto a autoridade certificadora.
