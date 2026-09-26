//! Desktop host for the review dashboard.
//!
//! The window is the same app the browser runs; this crate's only job is to make sure the
//! read-only API is there to answer it. It reuses a server that is already listening (the
//! usual case during development), and otherwise starts the bundled Node runtime against the
//! bundled API. The window stays hidden until the API answers, so the dashboard never opens
//! onto an error it is about to resolve on its own.
//!
//! It also tells that server what the bundle shipped with. A Windows bundle carries a Python
//! runtime under `resources/python/<triple>/`, and until the two were joined the server
//! resolved past it to a bare `python` — which on the machine the runtime was staged for, an
//! Install with no system Python, is nothing at all.

use std::env;
use std::fs;
use std::io::{BufRead, BufReader};
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::path::BaseDirectory;
use tauri::{Manager, RunEvent};

mod install;

/// Matches `shared/ports.ts` and the `VITE_API_BASE` the desktop bundle is built with.
const API_PORT: u16 = 5170;

/// A file holding one line: the path to the view database to open.
const VIEW_POINTER: &str = "view-path.txt";

struct ApiSidecar(Mutex<Option<Child>>);

fn api_is_listening() -> bool {
    let address: SocketAddr = SocketAddrV4::new(Ipv4Addr::LOCALHOST, API_PORT).into();
    TcpStream::connect_timeout(&address, Duration::from_millis(250)).is_ok()
}

/// `VENATOR_VIEW_DB` → `<app config>/view-path.txt` → this Install's own view, under the
/// application data directory the pipeline writes it to. Whichever wins is named in the
/// dashboard's header.
///
/// **None is a real answer and the ordinary one on a machine the app was just installed on.**
/// This used to end at a bundled copy of the dashboard's sample database — nineteen Postings
/// from real employers, none of them this person's — which meant a brand new Install opened
/// onto a full review queue of somebody else's job search. The API server refuses sample data
/// to a run that did not ask for it (`ui/server/db.ts`), so handing this path over would only
/// have been ignored; the honest thing is not to hand it over, and the bundle no longer
/// carries the file at all. With no view named, the server resolves its own and serves an
/// empty one, and the dashboard shows the screen written for an Install that has not run
/// anything yet.
fn resolve_view(app: &tauri::AppHandle) -> Option<PathBuf> {
    if let Ok(configured) = env::var("VENATOR_VIEW_DB") {
        if !configured.is_empty() {
            return Some(PathBuf::from(configured));
        }
    }

    if let Ok(config_dir) = app.path().app_config_dir() {
        if let Ok(text) = fs::read_to_string(config_dir.join(VIEW_POINTER)) {
            let pointed = PathBuf::from(text.trim());
            if pointed.is_file() {
                return Some(pointed);
            }
        }
    }

    // The shipped case, and the one this function used to miss outright: an Install with no
    // checkout builds its view under its application data directory, so the host resolves the
    // same store root the writer did (`src/venator/paths.py`).
    match install::install_view(&|name: &str| env::var(name).ok(), env::consts::OS) {
        Ok(view) if view.is_file() => return Some(view),
        Ok(view) => log::info!("this Install has built no view at {}", view.display()),
        Err(reason) => log::info!("this Install has no application data directory: {reason}"),
    }

    None
}

/// The Node runtime rides along as a Tauri sidecar, which lands beside the app executable.
///
/// It is staged as `binaries/node-<target triple><suffix>` and the bundler strips **only** the
/// triple when it copies it beside the executable, so the extension survives: `node.exe` on
/// Windows, a bare `node` everywhere else. `EXE_SUFFIX` is `""` off Windows, so this is the
/// same path it has always been there — but a Windows existence check does not append an
/// extension of its own, so joining a bare `node` found nothing and took the whole API start
/// down with it.
fn sidecar_runtime() -> Option<PathBuf> {
    let name = format!("node{}", std::env::consts::EXE_SUFFIX);
    let runtime = env::current_exe().ok()?.parent()?.join(name);
    runtime.is_file().then_some(runtime)
}

/// The Python interpreter this bundle ships, or None when it ships none.
///
/// Windows and Apple silicon bundles carry CPython, locked dependencies and `src/venator`
/// under `resources/python/<triple>/`. A developer build may carry none; in that case the
/// API server resolves an interpreter itself.
///
/// The file is looked for and nothing else is asked of it. Whether it *runs* is not settled
/// here on purpose: the only thing this could do with a bundled interpreter that fails to
/// start is fall back to whatever Python the machine happens to have, and an Install that
/// silently ran a system interpreter where it shipped its own would be lying about which code
/// it executed. The failure belongs on screen, where the dashboard names the interpreter it
/// was given and says it could not be started.
fn bundled_python(app: &tauri::AppHandle) -> Option<PathBuf> {
    // Said out loud rather than discarded into the same None a bundle with no runtime returns:
    // the two are different findings, and the caller's log line below would otherwise report a
    // staging that was never looked at as a staging that holds nothing.
    let resources = app
        .path()
        .resource_dir()
        .inspect_err(|error| {
            log::error!(
                "this bundle's resource directory could not be resolved, so the Python \
                 runtime it may carry could not be looked for: {error}"
            );
        })
        .ok()?;
    let interpreter = install::bundled_python(&resources, install::TARGET_TRIPLE);
    interpreter.is_file().then_some(interpreter)
}

/// The entry point Node can actually start from.
///
/// **This is the bug that meant a packaged Windows Install never served its API.** Tauri's
/// `resolve(…, BaseDirectory::Resource)` canonicalizes, and canonicalizing on Windows produces
/// the extended-length form — `\\?\C:\Users\…\resources\server.mjs`. Node reads its entry point
/// as a path to resolve rather than as a name to open: it sees the two leading backslashes,
/// takes the path for a UNC share, and walks the components until it calls `lstat` on `C:`,
/// which is a directory. It exits before loading a byte of the API:
///
/// ```text
/// Error: EISDIR: illegal operation on a directory, lstat 'C:'
///     at resolveMainPath (node:internal/modules/run_main:35:21)
/// ```
///
/// What a person saw was a window that opened onto a dashboard that never loaded, with the
/// host's own log saying it had started the API. Nothing said otherwise, because the sidecar's
/// stderr was inherited by a process with no console.
///
/// Only the drive-letter form is shortened, which is the whole of the case that breaks. A
/// verbatim UNC path (`\\?\UNC\server\share\…`) is left exactly as it is: shortening one of
/// those changes which file it names. The prefix is also what lifts the 260-character limit,
/// and dropping it gives that limit back — but this path is `<application data>\Venator\
/// resources\server.mjs`, which is nowhere near it, and a path Node refuses to start from is
/// worse than a long one it might.
fn node_entry_point(path: PathBuf) -> PathBuf {
    let text = path.to_string_lossy();
    let Some(rest) = text.strip_prefix(r"\\?\") else {
        return path.clone();
    };
    let bytes = rest.as_bytes();
    let drive = bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':';
    if drive {
        return PathBuf::from(rest.to_owned());
    }
    path.clone()
}

fn start_api(app: &tauri::AppHandle) -> Option<Child> {
    if api_is_listening() {
        log::info!("an API is already listening on {API_PORT}; reusing it");
        return None;
    }

    // Say why rather than returning None into the silence: the window still waits out its
    // deadline and then opens onto the dashboard's own "could not read the view database"
    // banner, which describes a missing database and not a missing runtime.
    let Some(runtime) = sidecar_runtime() else {
        log::error!(
            "no node{} beside the executable: the bundled runtime is missing, \
             so the API cannot be started",
            std::env::consts::EXE_SUFFIX
        );
        return None;
    };
    let server = node_entry_point(
        app.path()
            .resolve("resources/server.mjs", BaseDirectory::Resource)
            .ok()?,
    );
    let mut command = Command::new(&runtime);
    command.arg(server).env("VENATOR_API_PORT", API_PORT.to_string());

    // No view is not a failure to start against: it is what an Install that has never run the
    // pipeline has, and the server has its own answer for it. The variable is cleared rather
    // than left alone for the same reason `VENATOR_BUNDLED_PYTHON` is below — one already in
    // this process's environment came from somewhere else, and this host is what sets it.
    match resolve_view(app) {
        Some(view) => {
            log::info!("starting the API on {API_PORT} against {}", view.display());
            command.env("VENATOR_VIEW_DB", view);
        }
        None => {
            log::info!(
                "starting the API on {API_PORT} with no view: this Install has built none, \
                 so the dashboard opens on its first-run screen"
            );
            command.env_remove("VENATOR_VIEW_DB");
        }
    }

    // The bundled interpreter is announced, never imposed: `pythonInterpreter` in
    // `ui/server/locations.ts` puts `$VENATOR_PYTHON` ahead of it, so an Owner who named an
    // interpreter deliberately still gets theirs. Everything the server inherits — including a
    // `VENATOR_PYTHON` already in this process's environment — passes through untouched.
    match bundled_python(app) {
        Some(interpreter) => {
            log::info!("the API can run the pipeline with {}", interpreter.display());
            // The staged Playwright driver has no private Node. Hand its supported override
            // to the server so every Python child uses this bundle's signed Node sidecar.
            command.env("PLAYWRIGHT_NODEJS_PATH", &runtime);
            command.env(install::BUNDLED_PYTHON_ENV, interpreter);
        }
        None => {
            // Cleared rather than left alone. The variable is this host's to set, so one
            // already in the environment came from somewhere else — another bundle, a stale
            // shell — and a bundle that ships no runtime must not hand the server an
            // interpreter it does not have.
            command.env_remove(install::BUNDLED_PYTHON_ENV);
            // Not "this bundle carries none": `bundled_python` also answers None when the
            // resource directory could not be resolved at all, and it says so itself when it
            // does. What is established here is only that there is none to hand over.
            log::info!(
                "no bundled Python runtime for {} is available to hand over; the API resolves an interpreter itself",
                install::TARGET_TRIPLE
            );
        }
    }

    // The sidecar's own output, into this app's log, rather than inherited and lost.
    //
    // A windowed process has no console, so an inherited stdout goes nowhere: a Node runtime
    // that failed to start took its reason with it, and everything reaching anybody was a
    // dashboard that never loaded. That is not a hypothetical — CI has watched this app start,
    // log that it was starting the API, stay up, and never serve, with no record anywhere of
    // what the sidecar said. One reader thread per stream, so the lines arrive as they are
    // written rather than at exit, and a failure is readable in the same log file as
    // everything else this crate says.
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .inspect_err(|error| log::error!("could not start the API: {error}"))
        .ok()?;

    if let Some(output) = child.stdout.take() {
        thread::spawn(move || {
            for line in BufReader::new(output).lines().map_while(Result::ok) {
                log::info!("api: {line}");
            }
        });
    }
    if let Some(errors) = child.stderr.take() {
        thread::spawn(move || {
            for line in BufReader::new(errors).lines().map_while(Result::ok) {
                log::warn!("api: {line}");
            }
        });
    }

    Some(child)
}

/// Reveals the window once the API answers, or after the timeout so a failure is visible in
/// the dashboard's own error banner rather than as an app that never opens.
fn show_window_when_ready(app: tauri::AppHandle) {
    thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(8);
        while Instant::now() < deadline && !api_is_listening() {
            thread::sleep(Duration::from_millis(100));
        }
        if let Some(window) = app.get_webview_window("main") {
            let _ = window.show();
            let _ = window.set_focus();
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            // Registered in every build, and that is a deliberate change from registering it
            // only under `debug_assertions`.
            //
            // Every line this crate logs is about something a person cannot see and cannot
            // otherwise find out: which view database was resolved, whether the bundle carried
            // an interpreter, whether the sidecar was even found. A release build that dropped
            // all of it meant that an app opening onto a failure on somebody's machine left
            // nothing to read — and the people this ships to are exactly the people who cannot
            // reproduce it under a debugger. The log's default targets are this process's own
            // stdout and a file in this app's log directory; nothing is sent anywhere.
            app.handle().plugin(
                tauri_plugin_log::Builder::default()
                    .level(log::LevelFilter::Info)
                    .build(),
            )?;

            let handle = app.handle().clone();
            let child = start_api(&handle);
            app.manage(ApiSidecar(Mutex::new(child)));
            show_window_when_ready(handle);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the venator dashboard");

    app.run(|handle, event| {
        // The API belongs to this window; it should not outlive it.
        if let RunEvent::Exit = event {
            if let Some(sidecar) = handle.try_state::<ApiSidecar>() {
                if let Ok(mut slot) = sidecar.0.lock() {
                    if let Some(mut child) = slot.take() {
                        let _ = child.kill();
                    }
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    /// The exact string Tauri hands back on Windows, and the exact string Node needs.
    ///
    /// Written out rather than built by calling the function under test, and rather than
    /// obtained from `canonicalize` — an expectation computed by the code it checks agrees with
    /// that code whatever it does, and canonicalizing here would only answer for the machine the
    /// test runs on. This is what CI read out of a failing Install's own log.
    #[test]
    fn a_verbatim_drive_path_is_shortened_for_node() {
        assert_eq!(
            node_entry_point(PathBuf::from(r"\\?\C:\Users\owner\AppData\Local\Venator\resources\server.mjs")),
            PathBuf::from(r"C:\Users\owner\AppData\Local\Venator\resources\server.mjs")
        );
    }

    /// A share is not a drive, and shortening one changes which file it names.
    #[test]
    fn a_verbatim_unc_path_is_left_alone() {
        let share = PathBuf::from(r"\\?\UNC\server\share\Venator\resources\server.mjs");
        assert_eq!(node_entry_point(share.clone()), share);
    }

    /// Every other platform, and a Windows path that was never canonicalized.
    #[test]
    fn an_ordinary_path_is_handed_over_as_it_is() {
        for path in [
            r"C:\Users\owner\AppData\Local\Venator\resources\server.mjs",
            "/Applications/Venator.app/Contents/Resources/server.mjs",
            r"\\server\share\server.mjs",
        ] {
            assert_eq!(node_entry_point(PathBuf::from(path)), PathBuf::from(path));
        }
    }

    /// `\\?\` with nothing recognisable under it is not something to guess about.
    #[test]
    fn a_verbatim_path_with_no_drive_under_it_is_left_alone() {
        for path in [r"\\?\", r"\\?\C", r"\\?\Volume{9f8a}\server.mjs"] {
            assert_eq!(node_entry_point(PathBuf::from(path)), PathBuf::from(path));
        }
    }
}
