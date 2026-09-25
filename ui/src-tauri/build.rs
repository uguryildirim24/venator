fn main() {
    // The triple cargo is compiling this crate for, recorded so the host can name its own
    // staged runtime at runtime.
    //
    // `resources/python/<triple>/` is written by `tools/prepare-python.ts`, which takes the
    // triple from `TAURI_ENV_TARGET_TRIPLE` — the variable `tauri build` sets for every command
    // it runs, naming what it is packaging for. That is the same target cargo is handed here, so
    // the directory the bundler packed and the name this constant carries are one answer rather
    // than two that have to be reconciled.
    //
    // Read from a build script because there is no other way: `std::env::consts` gives an OS and
    // an architecture but never a triple, and `TARGET` is set for build scripts alone.
    println!(
        "cargo:rustc-env=VENATOR_TARGET_TRIPLE={}",
        std::env::var("TARGET").expect("cargo sets TARGET for every build script")
    );
    tauri_build::build()
}
