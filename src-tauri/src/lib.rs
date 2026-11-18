mod credentials;
mod exports;
mod process_tree;
mod supervisor;

use tauri::{Manager, State};

use credentials::CredentialStatus;
use supervisor::{BackendInfo, BackendStatus, DesktopState};

#[tauri::command]
fn backend_info(state: State<'_, DesktopState>) -> Result<BackendInfo, String> {
    state.info()
}

#[tauri::command]
fn backend_status(state: State<'_, DesktopState>) -> Result<BackendStatus, String> {
    state.status()
}

#[tauri::command]
async fn retry_backend(
    app: tauri::AppHandle,
    state: State<'_, DesktopState>,
) -> Result<BackendInfo, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.start(&app))
        .await
        .map_err(|_| "重启配音服务失败".to_string())?
}

#[tauri::command]
async fn set_credential(
    connection_id: String,
    api_key: String,
    state: State<'_, DesktopState>,
) -> Result<CredentialStatus, String> {
    credentials::validate_connection_id(&connection_id)?;
    if api_key.trim().is_empty() || api_key.len() > 4096 {
        return Err("请输入有效的 API Key".into());
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let saved = credentials::save(&connection_id, &api_key);
        let (persistent, message) = match saved {
            Ok(()) => (true, "API Key 已保存到系统凭据库".into()),
            Err(_) => (false, "仅本次使用，退出后需要重新输入 API Key".into()),
        };
        state.install_secret(connection_id, api_key, persistent, message)
    })
    .await
    .map_err(|_| "保存 API Key 失败".to_string())?
}

#[tauri::command]
async fn sync_credentials(
    connection_ids: Vec<String>,
    state: State<'_, DesktopState>,
) -> Result<Vec<CredentialStatus>, String> {
    if connection_ids.len() > 100 {
        return Err("连接数量过多".into());
    }
    for id in &connection_ids {
        credentials::validate_connection_id(id)?;
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut statuses = Vec::new();
        for id in connection_ids {
            if state.credential_status(&id).configured {
                statuses.push(state.credential_status(&id));
                continue;
            }
            match credentials::read(&id) {
                Ok(Some(key)) => statuses.push(state.install_secret(
                    id,
                    key,
                    true,
                    "已从系统凭据库加载 API Key".into(),
                )?),
                Ok(None) => statuses.push(state.credential_status(&id)),
                Err(message) => statuses.push(CredentialStatus {
                    connection_id: id,
                    configured: false,
                    storage: "unavailable".into(),
                    message,
                }),
            }
        }
        Ok(statuses)
    })
    .await
    .map_err(|_| "读取系统凭据失败".to_string())?
}

#[tauri::command]
async fn delete_credential(
    connection_id: String,
    state: State<'_, DesktopState>,
) -> Result<CredentialStatus, String> {
    credentials::validate_connection_id(&connection_id)?;
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        // Session-only keys were never successfully persisted; deleting them
        // must remain possible on Linux desktops without Secret Service.
        if state.should_delete_persistent_secret(&connection_id)? {
            credentials::delete(&connection_id)?;
        }
        state.remove_secret(&connection_id)
    })
    .await
    .map_err(|_| "清除连接密钥失败".to_string())?
}

#[tauri::command]
fn credential_status(
    connection_id: String,
    state: State<'_, DesktopState>,
) -> Result<CredentialStatus, String> {
    credentials::validate_connection_id(&connection_id)?;
    Ok(state.credential_status(&connection_id))
}

#[tauri::command]
async fn open_logs(state: State<'_, DesktopState>) -> Result<(), String> {
    let path = state.log_dir.clone();
    tauri::async_runtime::spawn_blocking(move || {
        open::that(path).map_err(|_| "无法打开日志文件夹".to_string())
    })
    .await
    .map_err(|_| "无法打开日志文件夹".to_string())?
}

#[tauri::command]
async fn open_data_dir(state: State<'_, DesktopState>) -> Result<(), String> {
    let path = state.data_dir.clone();
    tauri::async_runtime::spawn_blocking(move || {
        open::that(path).map_err(|_| "无法打开项目文件夹".to_string())
    })
    .await
    .map_err(|_| "无法打开项目文件夹".to_string())?
}

pub fn run() {
    let application = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            backend_info,
            backend_status,
            retry_backend,
            set_credential,
            sync_credentials,
            delete_credential,
            credential_status,
            open_logs,
            open_data_dir,
            exports::save_export,
            exports::open_export_folder,
        ])
        .setup(|app| {
            let data_dir = app.path().app_local_data_dir()?;
            let state = DesktopState::new(data_dir).map_err(std::io::Error::other)?;
            app.manage(state.clone());
            app.manage(exports::ExportState::default());
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let _ = state.start(&handle);
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("无法初始化 Jdx TTS Studio 桌面应用");
    application.run(|app, event| {
        if let tauri::RunEvent::Exit = event {
            app.state::<DesktopState>().shutdown();
        }
    });
}
