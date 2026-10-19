param([string]$Python = ".venv\Scripts\python.exe")

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot "src"

Write-Host "Abra a janela de selecao de certificado do PJeOffice e tecle Enter aqui."
[void][System.Console]::ReadLine()
& $Python -m pje_rpa inspect-signer --config config.toml
