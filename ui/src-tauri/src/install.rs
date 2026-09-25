//! Where this Install keeps its data — the Rust half of `src/venator/paths.py`.
//!
//! The pipeline writes its stores under one root, and since the Python side started
//! resolving that root a shipped Install's view lives in the application data directory, not
//! beside a source tree the Owner does not have. The desktop host has to reach the same file
//! the writer wrote, so this module mirrors `paths.install_data_dir` rule for rule:
//!
//! * `$VENATOR_HOME` overrides everything outright
//! * macOS — `~/Library/Application Support/Venator`
//! * Linux — `$XDG_DATA_HOME/venator` when that is an absolute path (the XDG basedir spec
//!   says to ignore a relative one), else `~/.local/share/venator`
//! * Windows — `%APPDATA%\Venator`
//!
//! Home comes from the environment and never from the account database, and a `~` in
//! `$VENATOR_HOME` is expanded against that same environment — both for the reason
//! `paths.py` gives: a path that belongs to a person must not be guessed from whoever
//! happens to be running the process.
//!
//! Everything here is `std` only and takes its environment as an argument, so the rules can
//! be exercised without a window, a webview, or a machine of a particular shape.

use std::path::{Path, PathBuf};

/// Overrides the platform directory outright. Same name the Python side reads.
pub const HOME_ENV: &str = "VENATOR_HOME";

/// The directory name on macOS and Windows, where application data is title-cased.
pub const DIRECTORY_NAME: &str = "Venator";

/// The directory name under XDG, which is lowercase.
pub const XDG_DIRECTORY_NAME: &str = "venator";

/// The view database under an Install's store root — `InstallStores.database_path`.
pub const VIEW_RELATIVE: &str = "build/venator.db";

/// A trimmed, non-empty environment value, or None. Empty means unset on both sides.
fn setting(read: &dyn Fn(&str) -> Option<String>, name: &str) -> Option<String> {
    read(name).map(|value| value.trim().to_owned()).filter(|value| !value.is_empty())
}

/// The home directory this environment names, from `HOME` or `%USERPROFILE%`.
fn home(read: &dyn Fn(&str) -> Option<String>) -> Option<String> {
    setting(read, "HOME").or_else(|| setting(read, "USERPROFILE"))
}

/// A configured path with a leading `~` expanded from the environment that was passed.
///
/// `~someone-else` is refused rather than resolved off the machine's account database: it is
/// another account's home directory, which is by definition not this Install's.
fn expand_home(value: &str, read: &dyn Fn(&str) -> Option<String>) -> Result<PathBuf, String> {
    if !value.starts_with('~') {
        return Ok(PathBuf::from(value));
    }
    let (head, rest) = match value.split_once('/') {
        Some((head, rest)) => (head, Some(rest)),
        None => (value, None),
    };
    if head != "~" {
        return Err(format!(
            "${HOME_ENV} is {value:?}: another account's home directory is not this Install's \
             — give the path in full"
        ));
    }
    let home = home(read).ok_or_else(|| {
        format!(
            "${HOME_ENV} is {value:?} but the environment names no home directory to expand \
             `~` against — give the path in full"
        )
    })?;
    Ok(match rest {
        Some(rest) => Path::new(&home).join(rest),
        None => PathBuf::from(home),
    })
}

/// The application data directory of this Install, or why it is unknowable.
///
/// `platform` is `std::env::consts::OS` in the app; an `Err` is a real answer rather than a
/// failure — it means the environment names no home and no override, so this Install has no
/// application data directory and the caller must look elsewhere.
pub fn install_data_dir(
    read: &dyn Fn(&str) -> Option<String>,
    platform: &str,
) -> Result<PathBuf, String> {
    if let Some(override_path) = setting(read, HOME_ENV) {
        return expand_home(&override_path, read);
    }
    if platform == "windows" {
        return setting(read, "APPDATA")
            .map(|appdata| Path::new(&appdata).join(DIRECTORY_NAME))
            .ok_or_else(|| format!("the environment names neither %APPDATA% nor ${HOME_ENV}"));
    }
    if platform == "macos" {
        return home(read)
            .map(|home| Path::new(&home).join("Library/Application Support").join(DIRECTORY_NAME))
            .ok_or_else(|| format!("the environment names neither $HOME nor ${HOME_ENV}"));
    }
    // The XDG basedir spec says a relative $XDG_DATA_HOME is invalid and must be ignored — and
    // a relative one here would resolve against whatever directory the app was launched from,
    // which is the failure mode this whole module exists to remove.
    if let Some(xdg) = setting(read, "XDG_DATA_HOME").filter(|xdg| Path::new(xdg).is_absolute()) {
        return Ok(Path::new(&xdg).join(XDG_DIRECTORY_NAME));
    }
    home(read)
        .map(|home| Path::new(&home).join(".local/share").join(XDG_DIRECTORY_NAME))
        .ok_or_else(|| format!("the environment names neither $HOME nor ${HOME_ENV}"))
}

/// The target triple this host was compiled for, recorded by `build.rs`.
///
/// It is the name `resources/python/<triple>/` is staged under, and it is the host's own answer
/// rather than a reading of what happens to be on disk. That is the whole point of taking it
/// from a compile-time constant: a bundle carrying a runtime staged for some *other* target is
/// a bundle with no runtime this host can use, and saying so is honest, where picking up the
/// single directory that happens to be there would run somebody else's interpreter. It is also
/// the case that cannot arise from a correct build — `prepare-sidecar.ts` clears runtimes staged
/// for another target before the bundler looks — and if it ever did, the packaging job catches
/// it: `.github/scripts/verify_desktop_installer.ps1` is passed the triple and asks the
/// installer for `resources\python\<triple>\python.exe` by name.
pub const TARGET_TRIPLE: &str = env!("VENATOR_TARGET_TRIPLE");

/// Where `bundle.resources` entries land under the resource directory.
///
/// Every entry in `tauri.conf.json` is declared under `resources/` and the bundler copies each
/// one preserving the path it is declared with, so `<resource dir>/resources/...` is where they
/// are found again. `resolve("resources/server.mjs", BaseDirectory::Resource)` in `lib.rs` is
/// the same join spelled Tauri's way, and the installer's own file list — read by the packaging
/// job — carries `resources\python\<triple>\python.exe`.
pub const RESOURCE_PREFIX: &str = "resources";

/// The one directory under `resources/` that every staged Python runtime lives in.
/// Matches `PYTHON_RESOURCES` in `ui/tools/python-runtime.ts`, which writes it.
pub const PYTHON_RESOURCES: &str = "python";

/// The interpreter inside a staged runtime.
///
/// Windows uses python.org's embeddable distribution; Apple silicon uses a
/// python-build-standalone install_only archive under python/bin/.
pub const PYTHON_EXECUTABLE: &str = "python.exe";

/// How the host tells the API server that this bundle ships an interpreter of its own.
///
/// Deliberately not `VENATOR_PYTHON`. That variable means *a person named this interpreter*, on
/// both sides of the seam, and the host is not a person: if it wrote its answer there, the
/// Owner's own setting would have to survive the host remembering not to overwrite it, and a
/// slip would discard their choice in silence. With two names the precedence is stated once, in
/// `pythonInterpreter` in `ui/server/locations.ts`, where `$VENATOR_PYTHON` still wins.
pub const BUNDLED_PYTHON_ENV: &str = "VENATOR_BUNDLED_PYTHON";

/// Where a runtime staged for `triple` keeps its interpreter, under a bundle's resource
/// directory. Path arithmetic only — whether anything is there is the caller's question.
pub fn bundled_python(resource_dir: &Path, triple: &str) -> PathBuf {
    let root = resource_dir.join(RESOURCE_PREFIX).join(PYTHON_RESOURCES).join(triple);
    if triple == "aarch64-apple-darwin" {
        root.join("python/bin/python3.13")
    } else {
        root.join(PYTHON_EXECUTABLE)
    }
}

/// The view database this Install's pipeline builds, or why the directory is unknowable.
pub fn install_view(
    read: &dyn Fn(&str) -> Option<String>,
    platform: &str,
) -> Result<PathBuf, String> {
    install_data_dir(read, platform).map(|dir| dir.join(VIEW_RELATIVE))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn environment(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> =
            pairs.iter().map(|(k, v)| ((*k).to_owned(), (*v).to_owned())).collect();
        move |name: &str| map.get(name).cloned()
    }

    fn resolved(pairs: &[(&str, &str)], platform: &str) -> String {
        install_data_dir(&environment(pairs), platform).expect("a data directory").display().to_string()
    }

    /// The segments joined the way *this host's* `std::path` joins them.
    ///
    /// `Path::join` inserts `MAIN_SEPARATOR`, which is `\` on Windows and `/` everywhere else,
    /// and it answers for the host rather than for the `platform` argument a case names — so on
    /// Linux the Windows branch produces a forward slash. That configuration never reaches an
    /// Install, because the app passes `std::env::consts::OS`; it reaches this module, which is
    /// the only place the two can differ.
    ///
    /// Written from the constant rather than obtained from `Path::join`, which is the call under
    /// test: an expectation assembled with it would agree with it whatever it did. Note that Rust
    /// leaves separators already inside a segment alone, so `"Library/Application Support"` stays
    /// as written on every host and only the join points move.
    fn joined(segments: &[&str]) -> String {
        segments.join(&std::path::MAIN_SEPARATOR.to_string())
    }

    /// A path this host agrees is absolute. `/data` is absolute on Unix and *relative* on Windows,
    /// where a rooted path with no drive letter is not absolute — and `install_data_dir` asks
    /// `Path::is_absolute`, so the two hosts genuinely disagree about that string. Written for the
    /// host so the branch under test is the one taken.
    const ABSOLUTE_XDG: &str = if cfg!(windows) { "D:\\data" } else { "/data" };

    #[test]
    fn macos_uses_application_support() {
        assert_eq!(
            resolved(&[("HOME", "/mock-macos-home/owner")], "macos"),
            joined(&["/mock-macos-home/owner", "Library/Application Support", "Venator"])
        );
    }

    #[test]
    fn linux_uses_xdg_when_absolute() {
        assert_eq!(
            resolved(&[("HOME", "/home/owner"), ("XDG_DATA_HOME", ABSOLUTE_XDG)], "linux"),
            joined(&[ABSOLUTE_XDG, "venator"])
        );
    }

    #[test]
    fn linux_ignores_a_relative_xdg() {
        assert_eq!(
            resolved(&[("HOME", "/home/owner"), ("XDG_DATA_HOME", "relative")], "linux"),
            joined(&["/home/owner", ".local/share", "venator"])
        );
    }

    #[test]
    fn windows_uses_appdata() {
        // The one expectation spelled out per host with nothing computed in it. The literal in the
        // `windows` arm is the path a Windows Install actually gets; the other is the only other
        // thing this code can produce. It used to carry the forward-slashed one unconditionally,
        // which meant this assertion would have failed outright the first time `cargo test` ran on
        // Windows — and until this leg was added, it never had.
        let expected = if cfg!(windows) {
            "C:\\Users\\owner\\AppData\\Roaming\\Venator"
        } else {
            "C:\\Users\\owner\\AppData\\Roaming/Venator"
        };
        assert_eq!(resolved(&[("APPDATA", "C:\\Users\\owner\\AppData\\Roaming")], "windows"), expected);
    }

    #[test]
    fn the_override_wins_on_every_platform() {
        for platform in ["macos", "linux", "windows"] {
            assert_eq!(
                resolved(&[("HOME", "/mock-macos-home/owner"), ("VENATOR_HOME", "/srv/venator")], platform),
                "/srv/venator"
            );
        }
    }

    #[test]
    fn an_empty_value_means_unset() {
        assert_eq!(
            resolved(&[("HOME", "/mock-macos-home/owner"), ("VENATOR_HOME", "  ")], "macos"),
            joined(&["/mock-macos-home/owner", "Library/Application Support", "Venator"])
        );
    }

    #[test]
    fn a_tilde_expands_against_the_environment_that_was_passed() {
        assert_eq!(
            resolved(&[("HOME", "/mock-macos-home/owner"), ("VENATOR_HOME", "~/elsewhere")], "macos"),
            joined(&["/mock-macos-home/owner", "elsewhere"])
        );
    }

    #[test]
    fn another_accounts_home_is_refused() {
        let error = install_data_dir(&environment(&[("VENATOR_HOME", "~someone/x")]), "macos")
            .expect_err("a refusal");
        assert!(error.contains("another account's home directory"), "{error}");
    }

    #[test]
    fn an_environment_naming_no_home_has_no_data_directory() {
        assert!(install_data_dir(&environment(&[]), "macos").is_err());
        assert!(install_data_dir(&environment(&[]), "linux").is_err());
        assert!(install_data_dir(&environment(&[]), "windows").is_err());
    }

    /// The triple is written by `build.rs` out of cargo's `TARGET`, so what it holds here is
    /// this host's own triple rather than a bundle's. Asserted against `consts::ARCH`, which is
    /// the one field of a triple `std` also knows: `x86_64-pc-windows-msvc` and
    /// `aarch64-apple-darwin` both lead with it. `consts::OS` is deliberately not asserted —
    /// it is `macos` where the triple says `darwin`, and a case that has to special-case its
    /// own expectation per platform is asserting the table, not the code.
    #[test]
    fn the_host_records_the_triple_it_was_compiled_for() {
        assert!(
            TARGET_TRIPLE.starts_with(std::env::consts::ARCH),
            "the recorded triple {TARGET_TRIPLE:?} does not name this host's architecture {:?}",
            std::env::consts::ARCH
        );
        assert!(
            TARGET_TRIPLE.matches('-').count() >= 2,
            "the recorded triple {TARGET_TRIPLE:?} is not a target triple"
        );
    }

    /// The join the whole wiring rests on, written out rather than assembled from the
    /// constants: an expectation built the way the code builds it agrees with it whatever it
    /// does. The literal is the path the packaging job reads out of the installer's own file
    /// list, with `/` for `\\` — `Path::join` writes this host's separator, and only the join
    /// points move.
    #[test]
    fn the_bundled_interpreter_sits_under_the_triple_the_host_was_built_for() {
        assert_eq!(
            bundled_python(Path::new("/opt/Venator"), "x86_64-pc-windows-msvc")
                .display()
                .to_string(),
            joined(&["/opt/Venator", "resources", "python", "x86_64-pc-windows-msvc", "python.exe"])
        );
    }

    /// The two names are not one name. A host that wrote its answer into `VENATOR_PYTHON`
    /// would be indistinguishable from the Owner having named an interpreter, which is the
    /// distinction `pythonInterpreter` in `ui/server/locations.ts` decides precedence by.
    #[test]
    fn the_bundled_interpreter_is_announced_under_its_own_name() {
        assert_eq!(BUNDLED_PYTHON_ENV, "VENATOR_BUNDLED_PYTHON");
        assert_ne!(BUNDLED_PYTHON_ENV, "VENATOR_PYTHON");
    }

    #[test]
    fn the_view_sits_under_build_in_the_store_root() {
        let view = install_view(&environment(&[("HOME", "/mock-macos-home/owner")]), "macos")
            .expect("a view path");
        assert_eq!(
            view.display().to_string(),
            joined(&["/mock-macos-home/owner", "Library/Application Support", "Venator", VIEW_RELATIVE])
        );
    }
}
