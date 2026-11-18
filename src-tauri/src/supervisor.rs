use std::collections::{HashMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::json;
use tauri::{AppHandle, Emitter, Manager};
use zeroize::Zeroizing;

use crate::credentials::CredentialStatus;
use crate::process_tree;

#[derive(Clone, Serialize)]
pub struct BackendInfo {
    pub base_url: String,
    pub token: String,
}

#[derive(Clone, Serialize)]
pub struct BackendStatus {
    pub state: String,
    pub message: String,
    pub base_url: Option<String>,
    pub pid: Option<u32>,
    pub log_dir: String,
}

#[derive(Deserialize)]
struct Handshake {
    #[serde(rename = "type")]
    kind: String,
    protocol: u32,
    port: u16,
    pid: u32,
}

struct ManagedChild {
    child: Child,
    stdin: ChildStdin,
    info: BackendInfo,
    #[cfg(target_os = "windows")]
    _job: process_tree::Job,
}

impl ManagedChild {
    fn send(&mut self, message: &serde_json::Value) -> Result<(), String> {
        serde_json::to_writer(&mut self.stdin, message)
            .map_err(|_| "无法向后端发送控制消息".to_string())?;
        self.stdin
            .write_all(b"\n")
            .and_then(|_| self.stdin.flush())
            .map_err(|_| "后端控制通道已断开".into())
    }
}

impl Drop for ManagedChild {
    fn drop(&mut self) {
        let _ = self.send(&json!({ "type": "shutdown" }));
        process_tree::terminate(&mut self.child);
    }
}

struct Inner {
    child: Option<ManagedChild>,
    status: BackendStatus,
    generation: u64,
    shutting_down: bool,
    session_keys: HashMap<String, Zeroizing<String>>,
    persisted_keys: HashSet<String>,
    credential_status: HashMap<String, CredentialStatus>,
}

#[derive(Clone)]
pub struct DesktopState {
    inner: Arc<Mutex<Inner>>,
    launch_lock: Arc<Mutex<()>>,
    pub data_dir: PathBuf,
    pub log_dir: PathBuf,
}

fn lock_error() -> String {
    "桌面状态不可用，请重新启动应用".into()
}

impl DesktopState {
    pub fn new(data_dir: PathBuf) -> Result<Self, String> {
        let log_dir = data_dir.join("logs");
        fs::create_dir_all(&log_dir).map_err(|_| "无法创建应用数据目录".to_string())?;
        #[cfg(target_os = "linux")]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = fs::set_permissions(&data_dir, fs::Permissions::from_mode(0o700));
            let _ = fs::set_permissions(&log_dir, fs::Permissions::from_mode(0o700));
        }
        Ok(Self {
            inner: Arc::new(Mutex::new(Inner {
                child: None,
                status: BackendStatus {
                    state: "starting".into(),
                    message: "正在启动配音服务".into(),
                    base_url: None,
                    pid: None,
                    log_dir: log_dir.to_string_lossy().into_owned(),
                },
                generation: 0,
                shutting_down: false,
                session_keys: HashMap::new(),
                persisted_keys: HashSet::new(),
                credential_status: HashMap::new(),
            })),
            launch_lock: Arc::new(Mutex::new(())),
            data_dir,
            log_dir,
        })
    }

    pub fn info(&self) -> Result<BackendInfo, String> {
        let guard = self.inner.lock().map_err(|_| lock_error())?;
        guard
            .child
            .as_ref()
            .filter(|_| guard.status.state == "ready")
            .map(|child| child.info.clone())
            .ok_or_else(|| guard.status.message.clone())
    }

    pub fn status(&self) -> Result<BackendStatus, String> {
        self.inner
            .lock()
            .map(|guard| guard.status.clone())
            .map_err(|_| lock_error())
    }

    fn publish(&self, app: &AppHandle, state: &str, message: String, generation: u64) {
        let status = if let Ok(mut guard) = self.inner.lock() {
            if guard.generation != generation || guard.shutting_down {
                return;
            }
            guard.status.state = state.into();
            guard.status.message = message;
            if state != "ready" {
                guard.status.base_url = None;
                guard.status.pid = None;
            }
            guard.status.clone()
        } else {
            return;
        };
        let _ = app.emit("backend-status", status);
    }

    pub fn shutdown(&self) {
        let child = if let Ok(mut guard) = self.inner.lock() {
            guard.shutting_down = true;
            guard.status.state = "stopped".into();
            guard.child.take()
        } else {
            None
        };
        drop(child);
    }

    pub fn credential_status(&self, id: &str) -> CredentialStatus {
        self.inner
            .lock()
            .ok()
            .and_then(|guard| guard.credential_status.get(id).cloned())
            .unwrap_or_else(|| CredentialStatus {
                connection_id: id.to_owned(),
                configured: false,
                storage: "none".into(),
                message: "尚未配置 API Key".into(),
            })
    }

    pub fn install_secret(
        &self,
        id: String,
        secret: String,
        persistent: bool,
        message: String,
    ) -> Result<CredentialStatus, String> {
        let mut guard = self.inner.lock().map_err(|_| lock_error())?;
        if persistent {
            guard.persisted_keys.insert(id.clone());
        }
        let status = CredentialStatus {
            connection_id: id.clone(),
            configured: true,
            storage: if persistent { "system" } else { "session" }.into(),
            message,
        };
        if let Some(child) = guard.child.as_mut() {
            child.send(&json!({"type": "secret", "connection_id": id, "key": secret}))?;
        }
        guard
            .session_keys
            .insert(id.clone(), Zeroizing::new(secret));
        guard.credential_status.insert(id, status.clone());
        Ok(status)
    }

    pub fn should_delete_persistent_secret(&self, id: &str) -> Result<bool, String> {
        let guard = self.inner.lock().map_err(|_| lock_error())?;
        Ok(guard.persisted_keys.contains(id)
            || !guard
                .credential_status
                .get(id)
                .is_some_and(|status| status.storage == "session"))
    }

    pub fn remove_secret(&self, id: &str) -> Result<CredentialStatus, String> {
        let mut guard = self.inner.lock().map_err(|_| lock_error())?;
        if let Some(child) = guard.child.as_mut() {
            child.send(&json!({"type":"secret", "connection_id":id, "key":""}))?;
        }
        guard.session_keys.remove(id);
        guard.persisted_keys.remove(id);
        let status = CredentialStatus {
            connection_id: id.to_owned(),
            configured: false,
            storage: "none".into(),
            message: "连接密钥已清除".into(),
        };
        guard
            .credential_status
            .insert(id.to_owned(), status.clone());
        Ok(status)
    }

    fn append_log(&self, line: &str) {
        let mut sanitized = line.to_owned();
        if let Ok(guard) = self.inner.lock() {
            for secret in guard.session_keys.values() {
                if !secret.is_empty() {
                    sanitized = sanitized.replace(secret.as_str(), "[REDACTED]");
                }
            }
            if let Some(child) = &guard.child {
                sanitized = sanitized.replace(&child.info.token, "[REDACTED]");
            }
        }
        if let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.log_dir.join("desktop-backend.log"))
        {
            let _ = writeln!(file, "{sanitized}");
        }
    }

    pub fn start(&self, app: &AppHandle) -> Result<BackendInfo, String> {
        let _launch = self.launch_lock.lock().map_err(|_| lock_error())?;
        let (old_child, generation) = {
            let mut guard = self.inner.lock().map_err(|_| lock_error())?;
            if guard.shutting_down {
                return Err("应用正在退出".into());
            }
            guard.generation += 1;
            (guard.child.take(), guard.generation)
        };
        drop(old_child);
        self.publish(app, "starting", "正在启动配音服务".into(), generation);
        let result = self.launch(app, generation);
        if let Err(message) = &result {
            self.append_log(message);
            self.publish(app, "failed", message.clone(), generation);
        }
        result
    }

    fn launch(&self, app: &AppHandle, generation: u64) -> Result<BackendInfo, String> {
        let mut command = backend_command(app)?;
        command
            .arg("--data-dir")
            .arg(&self.data_dir)
            .arg("--parent-watch")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        process_tree::prepare(&mut command);
        let mut child = command
            .spawn()
            .map_err(|error| format!("无法启动配音服务：{error}"))?;
        #[cfg(target_os = "windows")]
        let job = match process_tree::Job::attach(&child) {
            Ok(job) => job,
            Err(error) => {
                process_tree::terminate(&mut child);
                return Err(error);
            }
        };
        let mut stdin = child.stdin.take().ok_or("后端控制通道不可用")?;
        let stdout = child.stdout.take().ok_or("后端启动通道不可用")?;
        let stderr = child.stderr.take().ok_or("后端日志通道不可用")?;
        let log_state = self.clone();
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                log_state.append_log(&line);
            }
        });
        let token = random_token();
        let init = json!({"type": "init", "session_token": token});
        if serde_json::to_writer(&mut stdin, &init)
            .and_then(|_| {
                stdin.write_all(b"\n").map_err(serde_json::Error::io)?;
                stdin.flush().map_err(serde_json::Error::io)
            })
            .is_err()
        {
            process_tree::terminate(&mut child);
            return Err("无法初始化配音服务".into());
        }
        let (sender, receiver) = mpsc::sync_channel(1);
        thread::spawn(move || {
            let mut first = String::new();
            let result = BufReader::new(stdout).read_line(&mut first).map(|_| first);
            let _ = sender.send(result);
        });
        let handshake_result = receiver
            .recv_timeout(Duration::from_secs(40))
            .map_err(|_| "配音服务启动超时，请查看日志".to_string())
            .and_then(|line| line.map_err(|_| "读取后端启动信息失败".to_string()))
            .and_then(|line| validate_handshake(&line, child.id()));
        let handshake = match handshake_result {
            Ok(value) => value,
            Err(error) => {
                process_tree::terminate(&mut child);
                return Err(error);
            }
        };
        let info = BackendInfo {
            base_url: format!("http://127.0.0.1:{}", handshake.port),
            token,
        };
        let mut managed = ManagedChild {
            child,
            stdin,
            info: info.clone(),
            #[cfg(target_os = "windows")]
            _job: job,
        };
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(2))
            .no_proxy()
            .build()
            .map_err(|_| "无法创建本地健康检查连接".to_string())?;
        let deadline = Instant::now() + Duration::from_secs(15);
        let mut ready = false;
        while Instant::now() < deadline {
            if matches!(managed.child.try_wait(), Ok(Some(_))) {
                return Err("配音服务在启动时退出，请查看日志".into());
            }
            if client
                .get(format!("{}/api/v1/health", info.base_url))
                .bearer_auth(&info.token)
                .send()
                .is_ok_and(|response| response.status().is_success())
            {
                ready = true;
                break;
            }
            thread::sleep(Duration::from_millis(120));
        }
        if !ready {
            return Err("配音服务未通过健康检查，请查看日志后重试".into());
        }
        {
            let mut guard = self.inner.lock().map_err(|_| lock_error())?;
            if guard.shutting_down || guard.generation != generation {
                return Err("启动已取消".into());
            }
            for (id, secret) in &guard.session_keys {
                managed
                    .send(&json!({"type":"secret", "connection_id":id, "key":secret.as_str()}))?;
            }
            guard.status.base_url = Some(info.base_url.clone());
            guard.status.pid = Some(handshake.pid);
            guard.child = Some(managed);
        }
        self.publish(app, "ready", "配音服务已就绪".into(), generation);
        self.monitor(app.clone(), generation);
        Ok(info)
    }

    fn monitor(&self, app: AppHandle, generation: u64) {
        let state = self.clone();
        thread::spawn(move || loop {
            thread::sleep(Duration::from_millis(500));
            let exited = {
                let Ok(mut guard) = state.inner.lock() else {
                    return;
                };
                if guard.shutting_down || guard.generation != generation {
                    return;
                }
                match guard.child.as_mut() {
                    Some(child) => match child.child.try_wait() {
                        Ok(Some(status)) => {
                            Some(format!("配音服务异常退出（{status}），可重试启动"))
                        }
                        Err(_) => Some("无法读取配音服务状态，可重试启动".into()),
                        Ok(None) => None,
                    },
                    None => return,
                }
            };
            if let Some(message) = exited {
                let child = state
                    .inner
                    .lock()
                    .ok()
                    .and_then(|mut guard| guard.child.take());
                drop(child);
                state.append_log(&message);
                state.publish(&app, "failed", message, generation);
                return;
            }
        });
    }
}

fn random_token() -> String {
    rand::random::<[u8; 32]>()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn validate_handshake(line: &str, expected_pid: u32) -> Result<Handshake, String> {
    let value: Handshake =
        serde_json::from_str(line).map_err(|_| "配音服务返回了无效的启动信息".to_string())?;
    if value.kind != "ready" || value.protocol != 1 || value.port == 0 || value.pid != expected_pid
    {
        return Err("配音服务启动协议不匹配，请重新安装应用".into());
    }
    Ok(value)
}

fn backend_command(app: &AppHandle) -> Result<Command, String> {
    let executable = if cfg!(target_os = "windows") {
        "jdx-backend.exe"
    } else {
        "jdx-backend"
    };
    let packaged = app
        .path()
        .resolve(
            format!("backend/{executable}"),
            tauri::path::BaseDirectory::Resource,
        )
        .map_err(|_| "无法定位内置配音服务".to_string())?;
    if packaged.is_file() {
        let mut command = Command::new(&packaged);
        command.current_dir(packaged.parent().unwrap_or(Path::new(".")));
        return Ok(command);
    }
    #[cfg(debug_assertions)]
    {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or("工程路径无效")?;
        let python = root.join(if cfg!(target_os = "windows") {
            "backend/.venv/Scripts/python.exe"
        } else {
            "backend/.venv/bin/python"
        });
        if !python.is_file() {
            return Err("开发环境缺少 backend/.venv，请先运行项目初始化脚本".into());
        }
        let mut command = Command::new(python);
        command.arg("-m").arg("jdx_backend");
        command.current_dir(root.join("backend"));
        command.env("PYTHONPATH", root.join("backend"));
        Ok(command)
    }
    #[cfg(not(debug_assertions))]
    Err("安装包缺少配音服务，请重新安装完整版本".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn handshake_requires_expected_process_and_protocol() {
        assert!(validate_handshake(
            r#"{"type":"ready","protocol":1,"port":49152,"pid":321}"#,
            321
        )
        .is_ok());
        for value in [
            r#"{"type":"ready","protocol":1,"port":0,"pid":321}"#,
            r#"{"type":"ready","protocol":2,"port":49152,"pid":321}"#,
            r#"{"type":"ready","protocol":1,"port":49152,"pid":999}"#,
            "not-json",
        ] {
            assert!(validate_handshake(value, 321).is_err());
        }
    }

    #[test]
    fn launch_tokens_are_random_and_have_256_bits() {
        let one = random_token();
        let two = random_token();
        assert_eq!(one.len(), 64);
        assert!(one.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(one, two);
    }

    #[test]
    fn credential_removal_preserves_knowledge_of_earlier_system_key() {
        let root =
            std::env::temp_dir().join(format!("jdx-credential-state-{}", rand::random::<u64>()));
        let state = DesktopState::new(root.clone()).unwrap();
        state
            .install_secret("session".into(), "temporary".into(), false, String::new())
            .unwrap();
        assert!(!state.should_delete_persistent_secret("session").unwrap());
        state
            .install_secret("stored".into(), "old".into(), true, String::new())
            .unwrap();
        state
            .install_secret(
                "stored".into(),
                "new-temporary".into(),
                false,
                String::new(),
            )
            .unwrap();
        assert!(state.should_delete_persistent_secret("stored").unwrap());
        state.remove_secret("stored").unwrap();
        assert!(!state.credential_status("stored").configured);
        assert!(!state
            .inner
            .lock()
            .unwrap()
            .session_keys
            .contains_key("stored"));
        std::fs::remove_dir_all(root).unwrap();
    }
}
