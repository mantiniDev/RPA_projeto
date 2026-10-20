param(
    [string]$Tribunal = "",
    [string]$Python = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot "src"

# -Tribunal escolhe tribunais\<nome>.toml (ex.: tjpa, trt2); sem ele, usa config.toml.
if ($Tribunal) {
    $Config = Join-Path "tribunais" ("{0}.toml" -f $Tribunal.ToLower())
    if (-not (Test-Path -LiteralPath $Config)) {
        $disponiveis = (Get-ChildItem tribunais -Filter *.toml | ForEach-Object BaseName) -join ", "
        throw "Tribunal '$Tribunal' nao configurado. Disponiveis: $disponiveis"
    }
} else {
    $Config = "config.toml"
}
Write-Host "Configuracao: $Config"

Write-Host "Abra a janela de selecao de certificado do PJeOffice e tecle Enter aqui."
[void][System.Console]::ReadLine()
& $Python -m pje_rpa inspect-signer --config $Config
