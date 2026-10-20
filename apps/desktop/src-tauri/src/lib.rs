//! Tauri shell: open the same `/workflow` UI the browser uses (DESIGN §5).
//! No second frontend. Does not start Docker. Does not ship/refund.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

/// Resolve `http://127.0.0.1:${WORKFLOW_PORT:-8000}/workflow`.
pub fn workflow_url_from_env() -> String {
    let raw = std::env::var("WORKFLOW_PORT").unwrap_or_else(|_| "8000".to_string());
    let digits: String = raw.chars().filter(|c| c.is_ascii_digit()).collect();
    let port = if digits.is_empty() {
        "8000".to_string()
    } else {
        digits
    };
    format!("http://127.0.0.1:{port}/workflow")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let url = workflow_url_from_env();

    tauri::Builder::default()
        .setup(move |app| {
            use tauri::Manager;
            if let Some(window) = app.get_webview_window("main") {
                match url.parse::<tauri::Url>() {
                    Ok(parsed) => {
                        if let Err(err) = window.navigate(parsed) {
                            eprintln!("navigate to workflow failed: {err}");
                        }
                    }
                    Err(err) => eprintln!("invalid workflow url {url}: {err}"),
                }
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
