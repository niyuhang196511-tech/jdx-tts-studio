fn main() {
    // Source checkouts have no frozen runtime. Debug mode deliberately uses
    // the managed Python development environment; create an empty resource
    // staging directory so Tauri can compile before the first packaging run.
    if std::env::var("PROFILE").as_deref() == Ok("debug") {
        let path = std::path::Path::new("resources/backend");
        if !path.exists() {
            std::fs::create_dir_all(path)
                .expect("cannot create development resource staging directory");
        }
    }
    tauri_build::build()
}
