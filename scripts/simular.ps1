param(
    [ValidateSet("success", "captcha", "jus_postulandi", "second_factor", "already_registered")]
    [string]$Scenario = "success",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
& $Python -m pje_rpa simulate --config config.example.toml --data cadastro.example.json --scenario $Scenario

