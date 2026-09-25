<#
.SYNOPSIS
    Installs the Windows package the way a person installs it, starts it, and asks the running
    app's own API to answer.

.DESCRIPTION
    Every Windows defect this repository has found came from a step that *ran* something. The
    `VITE_API_BASE=` prefix that took the whole build down, `node` against `node.exe`,
    `Venator.exe` against `venator-dashboard.exe` — none of them is visible to a digest
    comparison, a PE header read or a file list taken out of an archive, and all three of those
    checks were green while each one was true.

    So this one installs the artifact and launches it. It establishes three claims, and they
    are different claims, reported separately so a failure names which one broke:

    1. **The files landed.** A silent install (`/S`) into the per-user location the bundle is
       configured for — `installMode` is NSIS's `currentUser` default, so there is no elevation
       prompt and no interactive session needed for the install itself. Where it landed is read
       out of the uninstall registry entry the installer wrote rather than assumed; if what that
       names does not hold the executable, the places a Windows install can land are searched
       for it and the route that found it is reported. Only when no route finds it at all is
       this a refusal — and both the entry the installer wrote and anything a virus scanner
       quarantined are printed, because a tree that installed and then vanished is a real
       failure mode and the two answers look identical from the outside. The tree is also
       waited for rather than demanded the instant the first process returned: an installer can
       outlive the process `Start-Process -Wait` waited on, and a run has already been red here
       with exit 0 and no tree. The parts the app cannot start without are then required by
       name.

    2. **The process started.** The installed executable is launched and is required to still be
       running at the moment the API answers. That ordering is the whole of the claim: an app
       that starts the sidecar and then dies leaves a server answering on the port, and a check
       that only polled the port would call that a pass.

    3. **The API answered, and answered as a brand new Install.** `GET /api/summary` and
       `GET /api/onboarding/state`, the two the dashboard reads before it draws anything, and
       their answers are asserted rather than merely received: nothing discovered, no Profile
       configured, and a view that is not the sample database. A packaged Install that opened
       onto nineteen Postings from employers nobody here searched for is a failure this
       repository has already had once.

    What it does **not** establish, and no job on a runner can: what a person sees. A window
    that opens onto a blank webview, an unreadable contrast, a control in the wrong place — all
    of that is invisible from here, and a green run of this script is not evidence about any of
    it. It says the package installs, the process runs, and the API behind the window answers
    correctly.

    One thing is asserted that is not about starting up: `POST` against the dashboard API must
    not be answered. That surface is `GET`-only by design (`ui/server/routes.ts`), and this is
    the only place in this repository where that is asked of an installed application rather
    than of the source.

    Nothing here reaches the network. Every request is to 127.0.0.1 on the port the host fixes.

    Every failure is a refusal with the finding named, and the diagnostics — the installed tree,
    what the installer registered, anything a virus scanner quarantined, the app's own log — are
    printed on the way out so a red run is readable without a second one. An API that never
    answers gets one more: the app's child processes, and then the bundled runtime started
    against the bundled API by hand with its output captured, because a sidecar that comes up
    that way and not under the host is a different failure from one that cannot come up at all.
#>

[CmdletBinding()]
param(
    # Where `tauri build` wrote the NSIS installer.
    [Parameter(Mandatory = $true)][string]$BundleDir,
    # The Rust target triple the bundle is named for; the staged runtime installs under it.
    [Parameter(Mandatory = $true)][string]$Triple,
    # `ui/src-tauri/tauri.conf.json`'s productName — the folder the per-user install lands in.
    [Parameter(Mandatory = $true)][string]$ProductName,
    # The Cargo package name, which is what the bundler names the executable.
    [Parameter(Mandatory = $true)][string]$Executable,
    # `ui/shared/ports.ts` / `lib.rs: API_PORT`. Fixed, so it is passed rather than discovered.
    [Parameter(Mandatory = $true)][int]$Port,
    # How long the app is given to install, start and answer, in seconds.
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'

$failures = New-Object System.Collections.Generic.List[string]
$origin = "http://127.0.0.1:$Port"

function Report([string]$label, [string]$detail) {
    Write-Host ("  {0,-16}{1}" -f $label, $detail)
}

function Require([bool]$ok, [string]$finding) {
    if (-not $ok) { $failures.Add($finding) }
}

function Test-PortListening {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connect = $client.BeginConnect([System.Net.IPAddress]::Loopback, $Port, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($connect)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# What the installer told Windows about itself. Read rather than assumed: `InstallLocation` is
# the installer's own answer to where it put the tree, and it is also the first thing worth
# printing when the tree is not there.
function Find-UninstallEntry {
    $roots = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        foreach ($key in Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue) {
            $entry = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
            if ($null -eq $entry) { continue }
            if ($entry.DisplayName -ne $ProductName) { continue }
            return [pscustomobject]@{ Key = $key.PSChildName; Entry = $entry }
        }
    }
    return $null
}

# Printed before the script gives up, so a red run carries what the next person would ask for
# rather than needing a second run to produce it.
function Show-Diagnostics([string]$installDir) {
    Write-Host "`nDiagnostics"
    if ($installDir -and (Test-Path -LiteralPath $installDir)) {
        Write-Host "  the installed tree"
        Get-ChildItem -LiteralPath $installDir -Recurse -File -ErrorAction SilentlyContinue |
            Select-Object -First 60 |
            ForEach-Object { Write-Host ("    {0,10}  {1}" -f $_.Length, $_.FullName.Substring($installDir.Length).TrimStart('\')) }
    } else {
        Write-Host "  the installed tree  — nothing at $installDir"
    }
    Write-Host "  what the installer registered"
    $registered = Find-UninstallEntry
    if ($null -eq $registered) {
        Write-Host "    no uninstall entry names $ProductName"
    } else {
        Write-Host "    $($registered.Key)"
        foreach ($name in @('DisplayName', 'DisplayVersion', 'InstallLocation', 'Publisher', 'UninstallString')) {
            Write-Host ("      {0,-18}{1}" -f $name, $registered.Entry.$name)
        }
    }
    # A tree that installed and then vanished is a real Windows failure mode, and the scanner is
    # the only thing that can say so. Asked, never relied on: a runner without one is normal.
    Write-Host "  what the virus scanner quarantined"
    $threats = @()
    try { $threats = @(Get-MpThreatDetection -ErrorAction Stop) } catch { $threats = @() }
    if ($threats.Count -eq 0) { Write-Host "    nothing, or nothing here to ask" }
    foreach ($threat in ($threats | Select-Object -First 10)) {
        Write-Host "    $($threat.ThreatID) $(@($threat.Resources) -join ', ')"
    }
    Write-Host "  processes named $Executable"
    $running = @(Get-Process -Name ([System.IO.Path]::GetFileNameWithoutExtension($Executable)) -ErrorAction SilentlyContinue)
    if ($running.Count -eq 0) { Write-Host "    none" }
    foreach ($process in $running) { Write-Host "    pid $($process.Id) started $($process.StartTime)" }
    Write-Host "  the app's own log"
    # tauri-plugin-log's LogDir target. Both roots are searched rather than one being reasoned
    # about: which of them Tauri resolves is its business, and a log that is not found is a
    # finding worth printing rather than an assumption worth making.
    $logs = @(
        "$env:LOCALAPPDATA\com.venator.dashboard\logs",
        "$env:APPDATA\com.venator.dashboard\logs"
    ) | Where-Object { Test-Path -LiteralPath $_ } | ForEach-Object { Get-ChildItem -LiteralPath $_ -Filter *.log -ErrorAction SilentlyContinue }
    if (@($logs).Count -eq 0) {
        Write-Host "    no log file was written"
    }
    foreach ($log in $logs) {
        Write-Host "    $($log.FullName)"
        Get-Content -LiteralPath $log.FullName -Tail 80 | ForEach-Object { Write-Host "      $_" }
    }
}

# Why the API did not come up, when it did not.
#
# The host spawns the sidecar with inherited stdio (`lib.rs: start_api`), and a windowed app
# has nowhere for that to go, so a Node process that died on startup dies silently and the only
# symptom reaching this script is a port that never opened. Rather than leave that to the next
# run, this asks the two questions that tell the causes apart: whether the host started a child
# process at all, and what the same sidecar says when it is started by hand with its output
# captured. A sidecar that comes up here and not there is the host's failure; one that fails
# here prints the reason.
#
# It is diagnostics and nothing else — it runs only on the way out of a run that is already
# red, and it asserts nothing.
function Show-SidecarAttempt([string]$installDir, $app) {
    Write-Host "`nWhy the API did not answer"
    if ($null -ne $app) {
        $app.Refresh()
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($app.Id)" -ErrorAction SilentlyContinue)
        if ($children.Count -eq 0) {
            Write-Host "  children        the app started no child process, so the sidecar never ran"
        }
        foreach ($child in $children) {
            Write-Host "  child           $($child.Name) (pid $($child.ProcessId))"
            Write-Host "                  $($child.CommandLine)"
        }
        # The port has to be free before the sidecar is started by hand, and the app is what
        # would be holding it.
        & taskkill.exe /PID $app.Id /T /F 2>&1 | Out-Null
        foreach ($attempt in 1..20) {
            if (-not (Test-PortListening)) { break }
            Start-Sleep -Milliseconds 500
        }
    }

    $node = Join-Path $installDir 'node.exe'
    $server = Join-Path $installDir 'resources\server.mjs'
    if (-not (Test-Path -LiteralPath $node) -or -not (Test-Path -LiteralPath $server)) {
        Write-Host "  by hand         the runtime or the API bundle is not in the installed tree, so there is nothing to start"
        return
    }
    $out = Join-Path $env:TEMP 'venator-sidecar-out.log'
    $errors = Join-Path $env:TEMP 'venator-sidecar-err.log'
    $env:VENATOR_API_PORT = "$Port"
    $sidecar = $null
    try {
        $sidecar = Start-Process -FilePath $node -ArgumentList $server -WorkingDirectory $installDir `
            -PassThru -RedirectStandardOutput $out -RedirectStandardError $errors
    } catch {
        Write-Host "  by hand         $node could not be started at all"
        Remove-Item Env:\VENATOR_API_PORT -ErrorAction SilentlyContinue
        return
    }
    $up = $false
    foreach ($attempt in 1..40) {
        if (Test-PortListening) { $up = $true; break }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "  by hand         the bundled runtime against the bundled API; listening on $Port : $up"
    $sidecar.Refresh()
    if (-not $sidecar.HasExited) { & taskkill.exe /PID $sidecar.Id /T /F 2>&1 | Out-Null }
    Remove-Item Env:\VENATOR_API_PORT -ErrorAction SilentlyContinue
    foreach ($stream in @(@{ Name = 'its output'; Path = $out }, @{ Name = 'its errors'; Path = $errors })) {
        Write-Host "  $($stream.Name)"
        $lines = @()
        if (Test-Path -LiteralPath $stream.Path) {
            $lines = @(Get-Content -LiteralPath $stream.Path -ErrorAction SilentlyContinue)
        }
        if ($lines.Count -eq 0) { Write-Host "    nothing" }
        foreach ($line in ($lines | Select-Object -First 40)) { Write-Host "    $line" }
    }
}

function Stop-With([string]$finding, [string]$installDir) {
    Show-Diagnostics $installDir
    Write-Host "::error::$finding"
    exit 1
}

# ---------------------------------------------------------------------------------------------
# 1. The files landed.
# ---------------------------------------------------------------------------------------------

Write-Host "`nWhat installing the package leaves on the machine"

$installers = @(Get-ChildItem -LiteralPath $BundleDir -Filter '*-setup.exe' -ErrorAction SilentlyContinue)
if ($installers.Count -ne 1) {
    Stop-With "expected exactly one *-setup.exe in $BundleDir and found $($installers.Count); there is no single package to install" ''
}
$installer = $installers[0]
Report 'package' "$($installer.Name) — $([math]::Round($installer.Length / 1MB, 1)) MB"

# Before anything is installed. A listener already on this port would be adopted by the host
# (`lib.rs: api_is_listening`), and every assertion below would then be about somebody else's
# server. There is no such thing on a fresh runner, which is exactly why an unexpected one is
# worth stopping for rather than working around.
if (Test-PortListening) {
    Stop-With "something is already listening on $origin before anything was installed; the app would adopt it and this script would be asserting against a server it did not start" ''
}
Report 'port' "$Port is free"

$startedInstalling = Get-Date
$install = Start-Process -FilePath $installer.FullName -ArgumentList '/S' -Wait -PassThru
if ($install.ExitCode -ne 0) {
    Stop-With "the silent install exited $($install.ExitCode); the package a person would double-click does not install" ''
}
Report 'installed' ("silently, exit 0 after {0:n1}s" -f ((Get-Date) - $startedInstalling).TotalSeconds)

# An NSIS install can outlive the process that was waited on. Windows relaunches an executable
# whose name looks like an installer, and `Start-Process -Wait` then waited for the launcher
# rather than for the install; the uninstall registry entry can also be written before the last
# file is. A run has already been red here with exit 0 and no tree, so the install is given time
# to finish appearing rather than asked about the instant the first process returned.
$installerName = [System.IO.Path]::GetFileNameWithoutExtension($installer.Name)
$stragglers = @(Get-Process -Name $installerName -ErrorAction SilentlyContinue)
if ($stragglers.Count -gt 0) {
    Report 'still running' "$($stragglers.Count) installer process(es) outlived the one that was waited on"
    Wait-Process -Name $installerName -Timeout $TimeoutSeconds -ErrorAction SilentlyContinue
}

# Where it landed, read out of what the installer itself registered — better evidence than a
# path this script would otherwise assume. A location is only accepted once the executable is
# actually in it, because the entry naming a directory that does not exist is exactly the
# failure this loop is here to wait out.
$installDir = ''
$route = ''
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ($true) {
    $registered = Find-UninstallEntry
    if ($null -ne $registered -and -not [string]::IsNullOrWhiteSpace($registered.Entry.InstallLocation)) {
        $candidate = $registered.Entry.InstallLocation.Trim('"').TrimEnd('\')
        if (Test-Path -LiteralPath (Join-Path $candidate $Executable)) {
            $installDir = $candidate
            $route = "the uninstall entry at $($registered.Key)"
            break
        }
    }
    if ((Get-Date) -ge $deadline) { break }
    Start-Sleep -Seconds 2
}

if ($installDir -eq '') {
    # Nothing the installer registered holds the app. Rather than assume the per-user default
    # and report its absence, the places a Windows install can land are searched for the
    # executable itself — the claim is that the files landed somewhere a person's machine would
    # have them, and the route that found them is reported either way.
    $searchRoots = @(
        $env:LOCALAPPDATA,
        (Join-Path $env:LOCALAPPDATA 'Programs'),
        $env:APPDATA,
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)}
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
    foreach ($root in $searchRoots) {
        $hit = @(Get-ChildItem -LiteralPath $root -Filter $Executable -Recurse -Depth 3 -File -ErrorAction SilentlyContinue)
        if ($hit.Count -gt 0) {
            $installDir = $hit[0].DirectoryName
            $route = "a search under $root, because nothing the installer registered holds $Executable"
            break
        }
    }
}

if ($installDir -eq '') {
    $registered = Find-UninstallEntry
    $named = if ($null -eq $registered) { 'no uninstall entry at all' } else { "an uninstall entry naming ""$($registered.Entry.InstallLocation)""" }
    Stop-With "the package reported success and left no $Executable anywhere a Windows install can land; it wrote $named" (Join-Path $env:LOCALAPPDATA $ProductName)
}
Report 'installed to' "$installDir — found via $route"

# The parts the app cannot start without, each named for what its absence would break. This is
# the same list `verify_desktop_installer.ps1` reads out of the NSIS archive, asked here of the
# installed tree — which is the only place the two can be told apart.
$parts = @(
    @{ Path = $Executable;                                       What = 'the desktop host' }
    @{ Path = 'node.exe';                                        What = 'the sidecar runtime that starts the API' }
    @{ Path = 'resources\server.mjs';                            What = 'the API bundle' }
    @{ Path = "resources\python\$Triple\python.exe";             What = 'the interpreter the pipeline runs on' }
    @{ Path = "resources\python\$Triple\src\venator\__init__.py"; What = 'the pipeline' }
    # The three adapters the bundled server spawns by path, beside itself. Asked here as well
    # as of the archive because this is the tree the app actually runs out of, and an adapter
    # the installer carried but did not lay down is a route that fails on a person's machine
    # with the pipeline present and working.
    @{ Path = 'resources\resolve_board.py';                     What = 'the board resolver onboarding spawns' }
    @{ Path = 'resources\verify_profile.py';                    What = 'the Profile load-back both write routes refuse a write without' }
    @{ Path = 'resources\plan.py';                              What = 'the run planner that states a run cost before it is started' }
)
foreach ($part in $parts) {
    $path = Join-Path $installDir $part.Path
    $item = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
    Require ($null -ne $item) "$($part.What) is not installed at $path"
    if ($null -ne $item) { Report 'landed' "$($part.Path) — $($item.Length) bytes" }
}
if ($failures.Count -gt 0) {
    foreach ($finding in $failures) { Write-Host "::error::$finding" }
    Stop-With "the installed tree is missing $($failures.Count) part(s) the app cannot start without" $installDir
}

$appPath = Join-Path $installDir $Executable
if (-not (Test-Path -LiteralPath $appPath)) {
    $found = (Get-ChildItem -LiteralPath $installDir -Filter '*.exe' | ForEach-Object { $_.Name }) -join ', '
    Stop-With "there is no $Executable in $installDir; the executables installed are: $found" $installDir
}

# ---------------------------------------------------------------------------------------------
# 2. The process started.
# ---------------------------------------------------------------------------------------------

Write-Host "`nWhat happens when it is started"

# The installer may leave the app running — its finish page offers to, and a silent run's
# behaviour there is the installer's business rather than this script's. Whatever it did, the
# process under test has to be the one this script starts, so anything already running is ended
# first and said out loud.
$leftovers = @(Get-Process -Name ([System.IO.Path]::GetFileNameWithoutExtension($Executable)) -ErrorAction SilentlyContinue)
if ($leftovers.Count -gt 0) {
    Report 'note' "the install left $($leftovers.Count) $Executable process(es) running; ending them so what is measured is this script's launch"
    foreach ($leftover in $leftovers) {
        & taskkill.exe /PID $leftover.Id /T /F 2>&1 | Out-Null
    }
    $freed = $false
    foreach ($attempt in 1..20) {
        if (-not (Test-PortListening)) { $freed = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $freed) {
        Stop-With "a server was still listening on $origin after the install's own app was ended, so a launch here would adopt it rather than start one" $installDir
    }
}

if (Test-PortListening) {
    Stop-With "something is listening on $origin before the app was started; the host adopts an existing server, so nothing below would be about this launch" $installDir
}

# `-WorkingDirectory` is the install directory deliberately. The API server resolves a checkout
# from where it is running (`ui/server/locations.ts`), and this job's working directory *is* a
# Venator checkout — inheriting it would let the packaged app resolve a store the machine it
# ships to does not have, and this script would be measuring the runner rather than the package.
$app = Start-Process -FilePath $appPath -WorkingDirectory $installDir -PassThru
Report 'started' "$Executable as pid $($app.Id), working directory $installDir"

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$answered = $false
$summaryBody = ''
while ((Get-Date) -lt $deadline) {
    $app.Refresh()
    if ($app.HasExited) {
        Stop-With "the app exited with code $($app.ExitCode) before the API answered; the installed package starts and does not stay up" $installDir
    }
    if (Test-PortListening) {
        try {
            $summaryBody = (Invoke-WebRequest -Uri "$origin/api/summary" -UseBasicParsing -TimeoutSec 10).Content
            $answered = $true
            break
        } catch {
            # The port is open before Hono is routing; keep waiting rather than reading a
            # connection reset as an answer.
        }
    }
    Start-Sleep -Milliseconds 500
}

if (-not $answered) {
    Show-SidecarAttempt $installDir $app
    Stop-With "the app was started and $origin/api/summary did not answer within $TimeoutSeconds seconds" $installDir
}

# The ordering is the claim. An app that starts a sidecar and then dies leaves a server
# answering on the port, and a check that polled the port alone would call that a pass.
$app.Refresh()
if ($app.HasExited) {
    Stop-With "the API answered but the app is no longer running (exit code $($app.ExitCode)); what is serving the dashboard outlived the window it belongs to" $installDir
}
Report 'running' "pid $($app.Id) is still up now that the API has answered"

$children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($app.Id)" -ErrorAction SilentlyContinue)
foreach ($child in $children) { Report 'child' "$($child.Name) (pid $($child.ProcessId))" }
Require ($children.Count -gt 0) "the app answered on $origin without a child process; the API is supposed to be the bundled sidecar this host starts"

# ---------------------------------------------------------------------------------------------
# 3. The API answered, and answered as a brand new Install.
# ---------------------------------------------------------------------------------------------

Write-Host "`nWhat the running app answers"

$summary = $null
try {
    $summary = $summaryBody | ConvertFrom-Json
} catch {
    Stop-With "GET /api/summary answered with something that is not JSON" $installDir
}
Require ($null -ne $summary.funnel) "GET /api/summary carried no funnel; the dashboard reads one before it draws anything"
Require ($null -ne $summary.database) "GET /api/summary named no database"
if ($null -ne $summary.funnel) {
    Report 'summary' "discovered $($summary.funnel.discovered), queued $($summary.funnel.queued), database $($summary.database.kind)"
    # The failure this repository has already had once: a packaged Install opening onto the
    # sample corpus. Asserted on the numbers rather than on the badge, because the badge is a
    # rendering and this is about what was served.
    Require ($summary.funnel.discovered -eq 0) "a freshly installed app reports $($summary.funnel.discovered) Postings discovered; nothing has run on this machine, so the only honest answer is none"
    Require ($summary.database.kind -ne 'fixture') "a freshly installed app is reading the sample database; it must resolve an empty view of its own"
}

$stateBody = ''
try {
    $stateBody = (Invoke-WebRequest -Uri "$origin/api/onboarding/state" -UseBasicParsing -TimeoutSec 10).Content
} catch {
    Stop-With "GET /api/onboarding/state did not answer; the app cannot decide between setup and the dashboard without it" $installDir
}
$state = $null
try {
    $state = $stateBody | ConvertFrom-Json
} catch {
    Stop-With "GET /api/onboarding/state answered with something that is not JSON" $installDir
}
Report 'state' "configured $($state.configured), implied '$($state.implied)', view $($state.view.kind)"
Require ($state.configured -eq $false) "a freshly installed app reports a Profile already configured; it must open on setup"
Require ($null -eq $state.implied) "a freshly installed app named an implied Profile; there is none on this machine"
Require ($null -ne $state.roots -and -not [string]::IsNullOrWhiteSpace($state.roots.write)) "the app named no directory it would write a Profile into, so setup would have nowhere to land"

# The dashboard API is `GET`-only, and this is the one place that is asked of an installed
# application rather than of the source. A 404 or a 405 is the answer; a 2xx is the finding.
$postStatus = 0
try {
    $postStatus = (Invoke-WebRequest -Uri "$origin/api/summary" -Method Post -UseBasicParsing -TimeoutSec 10 -SkipHttpErrorCheck).StatusCode
} catch {
    # An exception here is the request being refused, which is the outcome this asserts for.
    $postStatus = 405
}
Report 'post' "POST /api/summary answered $postStatus"
Require ($postStatus -ge 400) "POST /api/summary was answered $postStatus by the installed app; the dashboard API is read-only and GET-only"

# ---------------------------------------------------------------------------------------------

Write-Host "`nShutting the app down"
& taskkill.exe /PID $app.Id /T /F 2>&1 | Out-Null
$stopped = $false
foreach ($attempt in 1..20) {
    if (-not (Test-PortListening)) { $stopped = $true; break }
    Start-Sleep -Milliseconds 500
}
Report 'stopped' $(if ($stopped) { "the app and its API are gone from $Port" } else { "something is still listening on $Port" })
Require $stopped "the app was killed with its process tree and something is still listening on $origin; the API outlives the window it belongs to"

if ($failures.Count -gt 0) {
    Show-Diagnostics $installDir
    foreach ($finding in $failures) { Write-Host "::error::$finding" }
    Write-Host "`n$($failures.Count) finding(s)."
    exit 1
}

Write-Host "`nThe package installs, the process runs, and the API behind the window answers as a new Install."
Write-Host "This says nothing about what is drawn in that window; no runner can."
