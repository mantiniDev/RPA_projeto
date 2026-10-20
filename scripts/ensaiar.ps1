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

# Ensaio no PJe real: login com o A1, conferencia de nome/CPF/OAB e preenchimento do
# formulario. Para ANTES de "Confirmar dados": nada e enviado nem assinado.
# Abra o PJeOffice PRO ANTES desta etapa e deixe-o rodando na bandeja.
Start-Process "http://127.0.0.1:8723/"
& $Python -m pje_rpa serve --config $Config --ensaio
