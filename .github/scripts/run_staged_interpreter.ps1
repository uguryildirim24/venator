<#
.SYNOPSIS
    Runs the Python runtime the desktop bundle carries, and makes it prove it is the pipeline's.

.DESCRIPTION
    Everything this repository has ever asked about the staged runtime has been structural:
    a digest compared, a PE header read, an entry count, a file list taken out of the installer.
    All of it answers "the right bytes are in the right places". None of it answers "it runs" —
    and an embeddable CPython staged beside 129 MB of wheels it cannot import is a tree that
    passes every one of those questions and starts nothing.

    So this executes it. Three claims, each of which a correct file list can hold while being
    false:

    1. **It starts, and it is the interpreter this repository pinned.** The version is read out
       of `tools/python-runtime.ts` rather than written here, so a deliberate `PYTHON_VERSION`
       bump is not a red job and a runtime staged from some other archive is.

    2. **`import venator` resolves inside the staged tree.** The name alone proves nothing on a
       machine that has its own Python — the module's own `__file__` is what says which one was
       imported, and it has to be the `src/venator/` staged under this triple.

    3. **A third-party distribution imports, out of `Lib/site-packages`.** This is the `._pth`
       doing its job. The distribution's own path configuration names only the stdlib zip; a
       tree staged without the rewrite has every wheel next to an interpreter that cannot see
       one of them, and `import venator` alone would not notice, because the pipeline's
       top-level package imports nothing.

    Its `sys.path` is printed beside them, unasserted, and so is the interpreter's own
    `sys.executable`: what the `._pth` is for is that it is read instead of the environment, and
    the two claims above already prove that harder than a path list could — both imports landed
    inside this tree on a machine that has its own Python and its own PyYAML. Everything else
    printed here is asserted.

    Then the actual question the dashboard asks: `-m venator.llm.probe --lane <name> --json`, one
    of the three commands `ui/server/onboarding/runtime.ts` builds, run through the same
    interpreter the Tauri host now hands it. The runner has no `claude`, no `codex` and no key, so
    the lane comes back unavailable — that is the answer, and the probe exits 0 for it. What is
    being proved is that the Install can be asked at all, which is exactly what a Windows Install
    with no system Python could not do before the host and the server were joined.

    Its answer is asserted the way the dashboard reads it, and no harder. `readLane` refuses a
    lane with no name and a state it has never heard of, so this refuses both — the state list is
    read out of `ui/shared/onboarding.ts` rather than copied here, because a copy is a list that
    can drift into passing what the dashboard would report as a contract mismatch. The lane named
    in the answer has to be the lane that was asked for. **Which** state comes back is not
    asserted: whether a runtime is installed on a CI runner is not this script's business, and a
    guard that demanded `not_installed` would redden the day somebody installed one.

    Nothing here spends anything. A lane is probed by launching it; no runtime is installed on
    the runner, so each launch fails immediately and no completion is ever requested.

    Every failure is a refusal with the finding named. There is no flag that turns a question
    off.
#>

[CmdletBinding()]
param(
    # `ui/src-tauri`, the directory holding the staged `resources/`.
    [Parameter(Mandatory = $true)][string]$TauriRoot,
    # The Rust target triple the bundle is named for; the staged runtime lives under it.
    [Parameter(Mandatory = $true)][string]$Triple,
    # `ui/tools/python-runtime.ts`, which carries the version pin this reads back.
    [Parameter(Mandatory = $true)][string]$Pins,
    # `ui/shared/onboarding.ts`, which carries the runtime states the dashboard knows.
    [Parameter(Mandatory = $true)][string]$States,
    # The runtime lane to ask the probe for, so the answer can be checked against the question.
    [Parameter(Mandatory = $true)][string]$Lane
)

$ErrorActionPreference = 'Stop'

$failures = New-Object System.Collections.Generic.List[string]

function Report([string]$label, [string]$detail) {
    Write-Host ("  {0,-16}{1}" -f $label, $detail)
}

function Require([bool]$ok, [string]$finding) {
    if (-not $ok) { $failures.Add($finding) }
}

function Stop-With([string]$finding) {
    Write-Host "::error::$finding"
    exit 1
}

Write-Host "`nWhat the staged interpreter does when it is run"

$runtime = Join-Path $TauriRoot "resources/python/$Triple"
$python = Join-Path $runtime 'python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Stop-With "there is no interpreter to run at $python, so nothing about the staged runtime could be executed"
}
$runtimeFull = (Resolve-Path -LiteralPath $runtime).Path
$pythonFull = (Resolve-Path -LiteralPath $python).Path

# The pin, read out of the file that carries it rather than written down here. A check that
# spells its own expectation agrees with itself; this one has to agree with the repository.
if (-not (Test-Path -LiteralPath $Pins)) {
    Stop-With "there is no $Pins to read the pinned Python version out of"
}
$pinsText = Get-Content -LiteralPath $Pins -Raw
if ($pinsText -notmatch 'PYTHON_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"') {
    Stop-With "could not read PYTHON_VERSION out of $Pins"
}
$pinnedVersion = $Matches[1]
Report 'pinned' "Python $pinnedVersion, from $Pins"

# The dashboard's own list of runtime states, read for the same reason the version pin is: a
# check that writes down its own copy of the contract agrees with itself while the two sides
# drift apart. `readLane` renders a lane only when its state is one of these, so a state that
# is not one of these is a contract mismatch on screen — which is the failure this step exists
# to catch before an installer is uploaded.
if (-not (Test-Path -LiteralPath $States)) {
    Stop-With "there is no $States to read the runtime states the dashboard knows out of"
}
$statesText = Get-Content -LiteralPath $States -Raw
if ($statesText -notmatch '(?s)RUNTIME_STATES[^=]*=\s*\[(.*?)\]') {
    Stop-With "could not read RUNTIME_STATES out of $States"
}
$knownStates = @([regex]::Matches($Matches[1], '"([a-z_]+)"') | ForEach-Object { $_.Groups[1].Value })
if ($knownStates.Count -lt 1) {
    Stop-With "read RUNTIME_STATES out of $States and it named no states"
}
Report 'states' ($knownStates -join ', ')

# Written to a file rather than passed to `-c`: a multi-line program on a command line is a
# quoting problem in three shells at once, and the program is the least interesting part of
# this check. Everything it prints is a `KEY=value` line, so the parsing below is not a parser.
$program = Join-Path ([System.IO.Path]::GetTempPath()) "venator-staged-$PID.py"
@'
import json
import sys

import venator
import yaml

print("VERSION=" + ".".join(str(part) for part in sys.version_info[:3]))
print("EXECUTABLE=" + sys.executable)
print("VENATOR=" + str(venator.__file__))
print("YAML=" + str(yaml.__file__))
print("SYSPATH=" + json.dumps(sys.path))
'@ | Set-Content -LiteralPath $program -Encoding utf8

try {
    $lines = & $pythonFull $program
    $status = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $program -Force -ErrorAction SilentlyContinue
}

if ($status -ne 0) {
    Stop-With "the staged interpreter exited $status without importing the pipeline; the bundle carries a runtime that does not run"
}

$read = @{}
foreach ($line in $lines) {
    $split = $line.IndexOf('=')
    if ($split -gt 0) { $read[$line.Substring(0, $split)] = $line.Substring($split + 1) }
}
foreach ($key in @('VERSION', 'EXECUTABLE', 'VENATOR', 'YAML', 'SYSPATH')) {
    if (-not $read.ContainsKey($key)) {
        Stop-With "the staged interpreter ran but printed no $key line, so what it imported could not be read"
    }
}

Report 'ran' "$($read['EXECUTABLE']) — Python $($read['VERSION'])"
Require ($read['VERSION'] -eq $pinnedVersion) "the staged interpreter is Python $($read['VERSION']); this repository pins $pinnedVersion"

# `import venator` on a machine with its own Python proves nothing about which one was
# imported. The module's own file does.
$venatorFile = $read['VENATOR']
$expectedPipeline = Join-Path $runtimeFull 'src\venator\__init__.py'
Require ($venatorFile -eq $expectedPipeline) "the interpreter imported venator from $venatorFile, which is not the pipeline staged at $expectedPipeline"
Report 'imported' "venator from $venatorFile"

# A third-party distribution, out of the wheel set. This is the `._pth` rewrite being exercised
# rather than inspected: without it the interpreter sees the stdlib zip and nothing else.
$yamlFile = $read['YAML']
$site = Join-Path $runtimeFull 'Lib\site-packages'
# The separator is the whole of this check. `StartsWith($site)` alone is a prefix test on a
# string, and a prefix of a path is not an ancestor of it: a wheel set unpacked into a sibling
# `Lib\site-packages-evil`, or into `Lib\site-packagesX`, begins with those characters and is
# not this tree. Appending the separator is what makes it ancestry.
$siteRoot = $site.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
Require ($yamlFile.StartsWith($siteRoot, [System.StringComparison]::OrdinalIgnoreCase)) "the interpreter imported yaml from $yamlFile, which is not the staged wheel set under $site"
Report 'imported' "yaml from $yamlFile"

# Printed rather than asserted, and the distinction is worth stating. What `sys.path` is for is
# that the `._pth` is read *instead of* the environment, and the two assertions above already
# prove that harder than a path list could: `venator` and `yaml` were both imported out of this
# tree on a machine that has its own Python and its own PyYAML. Asserting the list itself would
# be asserting `site`'s behaviour, which is a different thing and one a wheel could change. It is
# in the log because it is the first thing anybody debugging this will want.
Report 'sys.path' ($read['SYSPATH'])

# --------------------------------------------------------------------------------------------

Write-Host "`nWhat the dashboard asks it"

# The command `ui/server/onboarding/runtime.ts` builds for a lane somebody picked, run through
# the interpreter the Tauri host now names for it. A lane is asked for by name rather than left
# to the probe's default, so the answer can be checked against the question. Exit 0 whenever the
# probe ran, including when the lane is unavailable — which is what a runner with no LLM runtime
# installed produces.
$probe = & $pythonFull -m venator.llm.probe --lane $Lane --json
$probeStatus = $LASTEXITCODE
if ($probeStatus -ne 0) {
    Stop-With "``python -m venator.llm.probe --lane $Lane --json`` exited $probeStatus on the staged runtime; the dashboard's runtime step cannot be answered by this bundle"
}

try {
    $lanes = @($probe -join "`n" | ConvertFrom-Json)
} catch {
    Stop-With "the runtime check answered something that is not JSON, so the dashboard could not read it"
}
Require ($lanes.Count -ge 1) 'the runtime check answered with no runtimes at all'
# `$reported` rather than `$lane`: PowerShell variable names are case-insensitive, so a loop
# variable called that *is* the `$Lane` parameter, and the question would be overwritten by the
# answer before it could be compared with it.
foreach ($reported in $lanes) {
    # The two fields `readLane` in ui/server/onboarding/runtime.ts refuses to guess at, held to
    # the same standard it holds them to: a name that is there, and a state it knows.
    $name = "$($reported.lane)"
    $state = "$($reported.state)"
    Require (($null -ne $reported.lane) -and ($name -ne '')) "the runtime check reported a runtime with no lane name, which the dashboard reports as a contract mismatch rather than rendering"
    Require ($knownStates -contains $state) "the runtime check reported lane '$name' in state '$state', which is not one of the states the dashboard knows ($($knownStates -join ', ')); the dashboard reports that as a contract mismatch rather than rendering"
    # The headline of this whole step. It is asserted rather than printed: an answer about some
    # other lane is an answer to a question nobody asked.
    Require ($name -eq $Lane) "the runtime check was asked for lane '$Lane' and answered for '$name'"
    Report 'runtime' "$name — $state"
}

# --------------------------------------------------------------------------------------------

if ($failures.Count -gt 0) {
    Write-Host ''
    foreach ($failure in $failures) { Write-Host "::error::$failure" }
    Write-Host "`n$($failures.Count) finding(s). The runtime this bundle carries is not one to hand anybody."
    exit 1
}

Write-Host "`nThe staged interpreter starts, is the version this repository pins, imports the"
Write-Host "pipeline and the wheel set out of the bundle, and answers the question the dashboard asks."
