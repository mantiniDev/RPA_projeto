param([string]$Python = ".venv\Scripts\python.exe")

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot "src"

# Ensaio no PJe real: login com o A1, conferencia de nome/CPF/OAB e preenchimento do
# formulario. Para ANTES de "Confirmar dados": nada e enviado nem assinado.
# Abra o PJeOffice PRO ANTES desta etapa e deixe-o rodando na bandeja.
Start-Process "http://127.0.0.1:8723/"
& $Python -m pje_rpa serve --config config.toml --ensaio
