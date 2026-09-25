<#
.SYNOPSIS
    Asks what actually landed in the Windows installer, rather than whether one was produced.

.DESCRIPTION
    A green `tauri build` is not evidence that the package works. The failure this repository
    already wrote a build-time refusal against — a bundle that ships `resources/python/` holding
    one README.txt, installs cleanly, opens onto the sample corpus on a machine with no Python
    and stays there — looks, from the outside, exactly like a good build: exit 0, an installer of
    a plausible-for-a-dashboard size, no warning anywhere. `desktop-stage.ts` refuses that bundle
    at staging time. This is the check on the other end: what the bundler consumed, and what came
    out the far side.

    Three questions, in the order the evidence gets stronger:

    1. **The staged tree the bundler consumed.** The interpreter, the wheel set and the pipeline,
       under the triple this bundle is named for, plus the verified Node sidecar and the API
       bundle. Anything absent here is a bundle shipping incomplete, and `desktop-stage.ts`
       should already have refused it — so a failure here is that refusal having been bypassed.

    2. **The installer is a real PE, of a plausible size.** An NSIS installer is a Windows
       executable; `MZ` and a `PE\0\0` at the offset the DOS header names is what makes it one
       rather than a file with an `.exe` on the end. The size band is a payload assertion in
       disguise: the dashboard and its webview host come to roughly ten megabytes,
       and the runtime is a hundred and fifty more. An installer under the floor is one that did
       not carry a runtime, whatever the staging directory said.

    3. **The payload, read out of the installer itself.** 7-Zip reads NSIS archives, so the file
       list can be taken from the artifact a person would download rather than from the tree it
       was built out of. This is the only one of the three that cannot be satisfied by a correct
       staging directory and a bundler that ignored it — so where it asks about a tree it asks
       for a count and not for one member, because one member is what that bundler produces.

    Every failure is a refusal with the finding named. Nothing here is skippable, and there is no
    flag that turns a question off: a check that can be waved past is a check that will be.
#>

[CmdletBinding()]
param(
    # `ui/src-tauri`, the directory holding `resources/` and `binaries/`.
    [Parameter(Mandatory = $true)][string]$TauriRoot,
    # The Rust target triple the bundle is named for; the staged runtime lives under it.
    [Parameter(Mandatory = $true)][string]$Triple,
    # Where `tauri build` wrote the NSIS installer.
    [Parameter(Mandatory = $true)][string]$BundleDir
)

$ErrorActionPreference = 'Stop'

$failures = New-Object System.Collections.Generic.List[string]

function Report([string]$label, [string]$detail) {
    Write-Host ("  {0,-16}{1}" -f $label, $detail)
}

function Require([bool]$ok, [string]$finding) {
    if (-not $ok) { $failures.Add($finding) }
}

# --------------------------------------------------------------------------------------------
# 1. The staged tree.
# --------------------------------------------------------------------------------------------

Write-Host "`nWhat the bundler consumed"

$runtime = Join-Path $TauriRoot "resources/python/$Triple"

# The same four parts `RUNTIME_EVIDENCE` in tools/python-runtime.ts names, asked of the tree
# after the bundle rather than before it. Spelled out here rather than imported, deliberately:
# this is the independent reading, and a check that shares its list with the thing it is
# checking agrees with it by construction.
$parts = @(
    @{ Path = 'python.exe';                   What = 'the interpreter' }
    @{ Path = 'Lib/site-packages';            What = 'the wheel set' }
    @{ Path = 'src/venator/__init__.py';      What = 'the pipeline' }
    @{ Path = 'src/venator/qualify/schemas/qualify-output-2.json'; What = 'the output schema the pipeline reads at import' }
)
foreach ($part in $parts) {
    $path = Join-Path $runtime $part.Path
    $present = Test-Path -LiteralPath $path
    Require $present ("$($part.What) is not staged: $($part.Path) is missing under resources/python/$Triple/")
    Report $(if ($present) { 'staged' } else { 'MISSING' }) "$($part.Path) — $($part.What)"
}

# The `._pth`, by glob rather than by `python313._pth`: its name carries the ABI number, so
# writing it down here would turn a legitimate `PYTHON_VERSION` bump into a red packaging job.
# What matters is that there is exactly one, because it is read *instead of* the environment —
# an embeddable interpreter with the distribution's own `._pth` has 129 MB of wheels beside it
# and cannot import one of them.
$pths = @(Get-ChildItem -LiteralPath $runtime -Filter 'python*._pth' -File -ErrorAction SilentlyContinue)
Require ($pths.Count -eq 1) "expected one python*._pth under resources/python/$Triple/, found $($pths.Count)"
Report $(if ($pths.Count -eq 1) { 'staged' } else { 'MISSING' }) "$(($pths.Name -join ', ')) — the path configuration it reads sys.path from"

# The wheels, counted. #60 stages fourteen runtime distributions out of uv.lock; the assertion is
# a floor rather than the number, so adding a dependency does not fail this, and staging none —
# a `uv pip install` that resolved to nothing and exited 0 — does.
$site = Join-Path $runtime 'Lib/site-packages'
if (Test-Path -LiteralPath $site) {
    $dists = @(Get-ChildItem -LiteralPath $site -Filter '*.dist-info' -Directory -ErrorAction SilentlyContinue)
    Require ($dists.Count -ge 10) "only $($dists.Count) distributions are staged under Lib/site-packages; the runtime wheel set is 14"
    Report 'wheels' "$($dists.Count) distributions: $((($dists | Select-Object -First 6).Name -join ', '))…"
}

# The pipeline, by weight rather than by its two required files: a `src/venator/` holding an
# `__init__.py` and nothing else satisfies the part check above and is not a pipeline.
$pipeline = Join-Path $runtime 'src/venator'
if (Test-Path -LiteralPath $pipeline) {
    $modules = @(Get-ChildItem -LiteralPath $pipeline -Filter '*.py' -Recurse -File)
    Require ($modules.Count -ge 40) "only $($modules.Count) Python modules are staged under src/venator/; the pipeline is far larger"
    Report 'pipeline' "$($modules.Count) modules under src/venator/"
}

# The sidecar, which `sidecar_runtime()` in src-tauri/src/lib.rs joins beside the executable, and
# the API bundle it is started against.
$sidecar = Join-Path $TauriRoot "binaries/node-$Triple.exe"
$sidecarPresent = Test-Path -LiteralPath $sidecar
Require $sidecarPresent "no verified Node sidecar at binaries/node-$Triple.exe"
if ($sidecarPresent) {
    $mb = [math]::Round((Get-Item -LiteralPath $sidecar).Length / 1MB, 1)
    Require ($mb -ge 40) "the staged sidecar is ${mb} MB, which is not a Node runtime"
    Report 'sidecar' "binaries/node-$Triple.exe, $mb MB"
}
# The sample view is deliberately not in this list: it is no longer staged and no longer
# shipped, because a shipped Install must not carry Postings that are not its Owner's
# (ui/tools/prepare-sidecar.ts, ui/server/db.ts).
#
# The three `.py` adapters are: the bundled server spawns each of them by path, beside itself,
# and an absent one is a route that answers an error on a machine where the pipeline is
# perfectly fine. `verify_profile.py` is the sharpest of the three — the two write routes refuse
# a write they cannot verify, so a bundle without it can set no Profile up at all — and
# `plan.py` is what states a run's cost before it starts. `tauri.conf.json` names all three
# explicitly, which the bundler enforces at build time; this is the second guard, on the
# artefact, and it is the one that runs on every build.
foreach ($resource in @(
    'resources/server.mjs',
    'resources/resolve_board.py',
    'resources/verify_profile.py',
    'resources/plan.py'
)) {
    $present = Test-Path -LiteralPath (Join-Path $TauriRoot $resource)
    Require $present "$resource is not staged"
    Report $(if ($present) { 'staged' } else { 'MISSING' }) $resource
}

# --------------------------------------------------------------------------------------------
# 2. The installer itself.
# --------------------------------------------------------------------------------------------

Write-Host "`nWhat came out"

$installers = @(Get-ChildItem -LiteralPath $BundleDir -Filter '*-setup.exe' -File -ErrorAction SilentlyContinue)
if ($installers.Count -ne 1) {
    Write-Host "::error::expected exactly one *-setup.exe under $BundleDir, found $($installers.Count)"
    exit 1
}
$installer = $installers[0]
$sizeMb = [math]::Round($installer.Length / 1MB, 1)
Report 'installer' "$($installer.Name), $sizeMb MB"

# A PE, read rather than assumed: `MZ`, then the `PE\0\0` signature at the offset the DOS header
# carries at 0x3C. A file that is not this is not something Windows will run, whatever produced it.
$stream = [System.IO.File]::OpenRead($installer.FullName)
try {
    $head = New-Object byte[] 0x40
    [void]$stream.Read($head, 0, $head.Length)
    Require ($head[0] -eq 0x4D -and $head[1] -eq 0x5A) 'the installer does not begin with MZ, so it is not a PE image'
    $peOffset = [System.BitConverter]::ToInt32($head, 0x3C)
    $stream.Position = $peOffset
    $signature = New-Object byte[] 4
    [void]$stream.Read($signature, 0, 4)
    $isPe = $signature[0] -eq 0x50 -and $signature[1] -eq 0x45 -and $signature[2] -eq 0 -and $signature[3] -eq 0
    Require $isPe "there is no PE\0\0 signature at the offset the DOS header names ($peOffset)"
    Report 'image' $(if ($isPe) { "PE\0\0 at 0x{0:X}" -f $peOffset } else { 'NOT A PE' })
} finally {
    $stream.Dispose()
}

# The size band, deliberately coarse: the itemised read of the archive below is the real
# assertion, and this one only has to catch the shape of a wrong answer. A dashboard and its
# webview host come to a few megabytes compressed; this installer
# carries an 89 MB Node and a 154 MB Python tree on top of that and comes out around 64. The
# floor sits well under that rather than at it, so a compression difference is not a red job.
# The ceiling catches the other direction: a build that packed a cargo target directory or a
# second runtime along with it.
Require ($sizeMb -ge 40) "the installer is $sizeMb MB, which is too small to be carrying the Node and Python runtimes"
Require ($sizeMb -le 400) "the installer is $sizeMb MB, far more than the staged payload accounts for"

# --------------------------------------------------------------------------------------------
# 3. The payload, read out of the installer.
# --------------------------------------------------------------------------------------------

Write-Host "`nWhat is inside it"

# 7-Zip reads NSIS archives, so this is the artifact a person downloads being asked what it
# holds — the one question a correct staging directory cannot answer on the bundler's behalf.
# No `2>&1`: a native command's stderr folded into the output stream is an ErrorRecord, and
# under `$ErrorActionPreference = 'Stop'` that has thrown rather than been captured. Let 7-Zip's
# own diagnostics go to the console and read the exit code.
$listing = & 7z l -ba -slt $installer.FullName
if ($LASTEXITCODE -ne 0) {
    Write-Host "::error::7z could not read $($installer.Name) as an NSIS archive (exit $LASTEXITCODE), so the payload could not be read out of the installer itself"
    exit 1
}
$names = @($listing | Where-Object { $_ -like 'Path = *' } | ForEach-Object { $_.Substring(7) })
Report 'entries' "$($names.Count) paths listed by 7z"

# The top of the tree, printed rather than asserted. It is where the host executable, the
# sidecar and the uninstaller sit, and a listing in the log is what tells the next person what
# those files are actually called — which is exactly what the first run of this job got wrong.
$roots = @($names | Where-Object { $_ -notmatch '\\' } | Sort-Object)
Report 'at the root' ($roots -join ', ')

# What the host executable is called, read out of the crate rather than written down here.
# `mainBinaryName` is unset in tauri.conf.json, so Tauri names the main binary after the Cargo
# package — `venator-dashboard` — and it is that file the bundler patches with the bundle type
# and packs. The first `name =` in the manifest is `[package]`'s; `[lib]`'s comes after it.
$manifest = Get-Content -LiteralPath (Join-Path $TauriRoot 'Cargo.toml') -Raw
if ($manifest -notmatch '(?m)^\s*name\s*=\s*"([^"]+)"') {
    Write-Host '::error::could not read the crate name out of src-tauri/Cargo.toml'
    exit 1
}
$hostBinary = $Matches[1]

# Named individually so a failure says which part of the payload is absent rather than that
# "something" is. The sidecar is `node.exe` in here, not `node-<triple>.exe`: `tauri-build`
# strips the triple when it places a sidecar beside the executable, which is what
# `sidecar_runtime()` then joins.
#
# `Floor` is where "at least one" is not an answer. This block exists to catch a bundler that
# ignored its inputs, and a directory asked for one member is exactly what such a bundler
# passes: a single file out of `Lib/site-packages`, or a `src/venator/` holding nothing but
# its `__init__.py`, is not a payload. So the two entries that name trees carry the counted
# floors their staged-tree counterparts above have — the real installer holds 857 paths under
# `Lib/site-packages` and the 52 modules the tree staged — set well under those rather than at
# them, so a dependency bump is not a red job and an empty tree still is.
$expected = @(
    @{ Match = "*${hostBinary}.exe";                                     Floor = 1;   What = "the desktop host (${hostBinary}.exe)" }
    @{ Match = '*node.exe';                                              Floor = 1;   What = 'the Node sidecar' }
    @{ Match = "*resources\python\$Triple\python.exe";                   Floor = 1;   What = 'the Python interpreter' }
    # By glob rather than by `python313._pth`, for the reason the staged-tree check gives: the
    # name carries the ABI number. Asked of the archive as well as of the tree because it is
    # what makes the interpreter able to import the wheels packed beside it — an installer
    # carrying the interpreter and the wheel set but not this one file is a broken package
    # that, read as a file list, looks like a complete one.
    @{ Match = "*resources\python\$Triple\python*._pth";                 Floor = 1;   What = 'the path configuration the interpreter reads sys.path from' }
    @{ Match = "*resources\python\$Triple\src\venator\__init__.py";      Floor = 1;   What = 'the pipeline' }
    @{ Match = "*resources\python\$Triple\src\venator\*.py";             Floor = 40;  What = 'the pipeline modules'; Means = 'the staged tree carries 52 of them' }
    # The schema is read when `venator.qualify.versions` is imported and is not a dependency,
    # so a bundle without it fails on the Owner's machine. Staging refuses that tree; this
    # asks the package.
    @{ Match = "*resources\python\$Triple\src\venator\qualify\schemas\qualify-output-2.json"; Floor = 1; What = 'the output schema the pipeline reads at import' }
    @{ Match = "*resources\python\$Triple\Lib\site-packages\*";          Floor = 400; What = 'the wheel set'; Means = 'the fourteen staged distributions unpack to around 857 paths' }
    @{ Match = '*resources\server.mjs';                                  Floor = 1;   What = 'the read-only API' }
    # The three adapters the bundled server spawns by path, beside itself. Every one of them is
    # a route that stops working without ever saying the file is missing: `resolve_board.py`
    # turns a pasted careers page into a board, `verify_profile.py` is the load-back both write
    # routes refuse a write without — so a bundle without it can set up no Profile at all — and
    # `plan.py` is what states what a run would do before a person starts one.
    @{ Match = '*resources\resolve_board.py';                            Floor = 1;   What = 'the board resolver onboarding spawns' }
    @{ Match = '*resources\verify_profile.py';                           Floor = 1;   What = 'the Profile load-back both write routes refuse a write without' }
    @{ Match = '*resources\plan.py';                                     Floor = 1;   What = 'the run planner that states a run cost before it is started' }
)
foreach ($want in $expected) {
    $hit = @($names | Where-Object { $_ -like $want.Match })
    $present = $hit.Count -ge 1
    Require $present "the installer does not hold $($want.What): nothing matching $($want.Match)"
    if ($present -and $want.Floor -gt 1) {
        Require ($hit.Count -ge $want.Floor) "the installer holds only $($hit.Count) path(s) of $($want.What); $($want.Means)"
    }
    Report $(if (-not $present) { 'MISSING' } elseif ($hit.Count -lt $want.Floor) { 'THIN' } else { 'inside' }) "$($want.What) — $($hit.Count) path(s) matching $($want.Match)"
}

# --------------------------------------------------------------------------------------------

if ($failures.Count -gt 0) {
    Write-Host ''
    foreach ($failure in $failures) { Write-Host "::error::$failure" }
    Write-Host "`n$($failures.Count) finding(s). The installer this run produced is not one to hand anybody."
    exit 1
}

Write-Host "`nThe installer carries the desktop host, the verified Node sidecar, the read-only API,"
Write-Host "the pinned interpreter, the wheel set and the pipeline."
