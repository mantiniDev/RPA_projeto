param([string]$Python = "python")

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath ".venv")) { & $Python -m venv .venv }
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -e ".[browser,signer]"
& ".venv\Scripts\python.exe" -m playwright install msedge

if (-not (Test-Path -LiteralPath "config.toml")) { Copy-Item "config.example.toml" "config.toml" }

Write-Host "Preparacao concluida."
Write-Host "1) Edite config.toml (tribunal, URL oficial, seletores homologados, [signer])."
Write-Host "2) Levante os nomes das janelas do PJeOffice: .\scripts\inspecionar-assinador.ps1"
Write-Host "3) Rode a pagina local: .\scripts\executar-live.ps1"
