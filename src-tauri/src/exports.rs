use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use reqwest::blocking::Client;
use reqwest::Url;
use serde::Serialize;
use tauri::{AppHandle, State};
use tauri_plugin_dialog::DialogExt;

use crate::supervisor::DesktopState;

#[derive(Clone, Default)]
pub struct ExportState(Arc<Mutex<Option<PathBuf>>>);

#[derive(Serialize)]
pub struct SaveResult {
    saved: bool,
    path: Option<String>,
    message: String,
}

fn validate_media_url(value: &str, base: &str) -> Result<Url, String> {
    let url = Url::parse(value).map_err(|_| "导出文件地址无效，请重新导出".to_string())?;
    let base = Url::parse(base).map_err(|_| "配音服务地址不可用".to_string())?;
    let segments: Vec<_> = url
        .path()
        .split('/')
        .filter(|part| !part.is_empty())
        .collect();
    let valid_path = segments.len() == 5
        && segments[..3] == ["api", "v1", "media"]
        && segments[3..].iter().all(|part| {
            part.chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        });
    if base.scheme() != "http"
        || base.host_str() != Some("127.0.0.1")
        || url.origin() != base.origin()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || !valid_path
    {
        return Err("只能保存当前配音服务生成的导出文件".into());
    }
    Ok(url)
}

fn safe_filename(suggested: &str) -> String {
    let name: String = suggested
        .chars()
        .filter(|c| !c.is_control() && !"/\\:*?\"<>|".contains(*c))
        .take(180)
        .collect();
    let name = name.trim_matches(['.', ' ']);
    if name.is_empty() {
        "配音导出.wav".into()
    } else {
        name.into()
    }
}

fn write_new_file(path: &Path, reader: &mut impl Read) -> Result<(), String> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|error| {
            if error.kind() == std::io::ErrorKind::AlreadyExists {
                "该位置已有同名文件。原文件未修改，请重新保存并使用其他名称。".to_string()
            } else {
                "无法创建导出文件，请检查文件夹权限和剩余空间".to_string()
            }
        })?;
    // The local export source is immutable. On a failed transfer, remove only
    // the new file that this operation created, never an existing user file.
    let result = std::io::copy(reader, &mut file).and_then(|size| {
        if size == 0 {
            Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "empty export",
            ))
        } else {
            file.flush().and_then(|_| file.sync_all())
        }
    });
    drop(file);
    if result.is_err() {
        let _ = fs::remove_file(path);
        return Err("导出保存未完成，请检查磁盘空间或重新导出".into());
    }
    Ok(())
}

#[tauri::command]
pub async fn save_export(
    url: String,
    suggested_name: String,
    app: AppHandle,
    state: State<'_, DesktopState>,
    exports: State<'_, ExportState>,
) -> Result<SaveResult, String> {
    let info = state.info()?;
    let url = validate_media_url(&url, &info.base_url)?;
    let suggested_name = safe_filename(&suggested_name);
    let exports = exports.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let selected = app
            .dialog()
            .file()
            .set_file_name(suggested_name)
            .blocking_save_file();
        let Some(selected) = selected else {
            return Ok(SaveResult {
                saved: false,
                path: None,
                message: "已取消保存".into(),
            });
        };
        let path = selected
            .into_path()
            .map_err(|_| "请选择本地文件夹".to_string())?;
        let client = Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(600))
            .build()
            .map_err(|_| "无法初始化导出保存".to_string())?;
        let mut response = client
            .get(url)
            .bearer_auth(&info.token)
            .send()
            .map_err(|_| "无法读取导出音频，请检查本地配音服务是否正常".to_string())?;
        if !response.status().is_success() {
            return Err("导出文件不可用或访问凭据已过期，请重新导出".into());
        }
        write_new_file(&path, &mut response)?;
        if let Ok(mut last) = exports.0.lock() {
            *last = Some(path.clone());
        }
        Ok(SaveResult {
            saved: true,
            path: Some(path.to_string_lossy().into_owned()),
            message: "导出文件已保存".into(),
        })
    })
    .await
    .map_err(|_| "保存导出文件失败".to_string())?
}

#[tauri::command]
pub async fn open_export_folder(exports: State<'_, ExportState>) -> Result<(), String> {
    let path = exports
        .0
        .lock()
        .map_err(|_| "无法读取导出位置".to_string())?
        .as_ref()
        .and_then(|path| path.parent())
        .map(Path::to_owned)
        .ok_or_else(|| "请先保存一个导出文件".to_string())?;
    tauri::async_runtime::spawn_blocking(move || {
        open::that(path).map_err(|_| "无法打开导出文件夹".to_string())
    })
    .await
    .map_err(|_| "无法打开导出文件夹".to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_current_local_media_origin_is_accepted() {
        let base = "http://127.0.0.1:35123";
        assert!(validate_media_url(
            "http://127.0.0.1:35123/api/v1/media/project-1/asset_1?signature=abc&expires=1",
            base
        )
        .is_ok());
        for bad in [
            "https://example.com/api/v1/media/p/a",
            "http://127.0.0.1:35124/api/v1/media/p/a",
            "http://localhost:35123/api/v1/media/p/a",
            "http://127.0.0.1:35123/api/v1/connections",
            "http://127.0.0.1:35123/api/v1/media/p/%2fetc",
            "http://user:pass@127.0.0.1:35123/api/v1/media/p/a",
            "http://127.0.0.1:35123/api/v1/media/p/a#fragment",
        ] {
            assert!(validate_media_url(bad, base).is_err(), "{bad}");
        }
    }

    #[test]
    fn saving_preserves_existing_files_and_removes_failed_new_files() {
        let root = std::env::temp_dir().join(format!("jdx-save-test-{}", rand::random::<u64>()));
        fs::create_dir(&root).unwrap();
        let target = root.join("已有音频.wav");
        fs::write(&target, b"original").unwrap();
        assert!(write_new_file(&target, &mut &b"replacement"[..]).is_err());
        assert_eq!(fs::read(&target).unwrap(), b"original");
        let empty = root.join("empty.wav");
        assert!(write_new_file(&empty, &mut &b""[..]).is_err());
        assert!(!empty.exists());
        let saved = root.join("新的音频.wav");
        write_new_file(&saved, &mut &b"audio"[..]).unwrap();
        assert_eq!(fs::read(saved).unwrap(), b"audio");
        fs::remove_dir_all(root).unwrap();
    }
}
