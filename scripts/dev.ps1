param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Rest
)

$ErrorActionPreference = 'Stop'
$src = (Resolve-Path (Join-Path $PSScriptRoot '..\src')).Path
$env:PYTHONPATH = $src
python -m skysherpa @Rest
exit $LASTEXITCODE
