use std::sync::Arc;

use keyring_core::Entry;
use serde::Serialize;

const SERVICE: &str = "com.jdx.ttsstudio.cloud";

#[derive(Clone, Serialize)]
pub struct CredentialStatus {
    pub connection_id: String,
    pub configured: bool,
    pub storage: String,
    pub message: String,
}

pub fn validate_connection_id(id: &str) -> Result<(), String> {
    if id.is_empty()
        || id.len() > 128
        || !id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return Err("连接标识无效".into());
    }
    Ok(())
}

fn entry(connection_id: &str) -> Result<Entry, String> {
    validate_connection_id(connection_id)?;
    #[cfg(target_os = "linux")]
    let store: Arc<keyring_core::CredentialStore> = zbus_secret_service_keyring_store::Store::new()
        .map_err(|_| "系统钥匙环不可用或尚未解锁".to_string())?;
    #[cfg(target_os = "windows")]
    let store: Arc<keyring_core::CredentialStore> = windows_native_keyring_store::Store::new()
        .map_err(|_| "Windows 凭据管理器不可用".to_string())?;
    #[cfg(not(any(target_os = "linux", target_os = "windows")))]
    return Err("当前系统不支持持久化凭据".into());
    #[cfg(any(target_os = "linux", target_os = "windows"))]
    store
        .build(SERVICE, connection_id, None)
        .map_err(|_| "无法访问系统凭据项".into())
}

pub fn save(connection_id: &str, secret: &str) -> Result<(), String> {
    if secret.trim().is_empty() || secret.len() > 4096 {
        return Err("请输入有效的 API Key".into());
    }
    entry(connection_id)?
        .set_password(secret)
        .map_err(|_| "无法保存到系统凭据库，本次会话仍可使用".into())
}

pub fn read(connection_id: &str) -> Result<Option<String>, String> {
    match entry(connection_id)?.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring_core::Error::NoEntry) => Ok(None),
        Err(_) => Err("无法读取系统凭据，请解锁钥匙环或重新输入 API Key".into()),
    }
}

pub fn delete(connection_id: &str) -> Result<(), String> {
    match entry(connection_id)?.delete_credential() {
        Ok(()) | Err(keyring_core::Error::NoEntry) => Ok(()),
        Err(_) => Err("无法删除系统凭据，请解锁钥匙环后重试；连接配置已保留".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credential_ids_cannot_be_paths_or_unbounded() {
        assert!(validate_connection_id("account-123_abc").is_ok());
        for id in ["", "../outside", "a/b", "a\\b", "a\nsecret"] {
            assert!(validate_connection_id(id).is_err());
        }
        assert!(validate_connection_id(&"a".repeat(129)).is_err());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn unavailable_session_bus_never_persists_credentials() {
        // Isolate the D-Bus environment in a subprocess so parallel tests and
        // the user's real Secret Service are never touched.
        if std::env::var_os("JDX_KEYRING_TEST_CHILD").is_some() {
            assert!(save("unavailable-keyring-test", "test-only-not-a-real-key").is_err());
            assert!(read("unavailable-keyring-test").is_err());
            assert!(delete("unavailable-keyring-test").is_err());
            return;
        }
        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "credentials::tests::unavailable_session_bus_never_persists_credentials",
            ])
            .env("JDX_KEYRING_TEST_CHILD", "1")
            .env(
                "DBUS_SESSION_BUS_ADDRESS",
                "unix:path=/nonexistent/jdx-test-bus",
            )
            .status()
            .expect("run isolated keyring failure test");
        assert!(status.success());
    }
}
