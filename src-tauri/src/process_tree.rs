use std::process::{Child, Command};
use std::thread;
use std::time::{Duration, Instant};

pub fn prepare(command: &mut Command) {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::System::Threading::{CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW};
        command.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
    }
}

pub fn terminate(child: &mut Child) {
    #[cfg(target_os = "linux")]
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGTERM);
    }
    #[cfg(not(target_os = "linux"))]
    let _ = child.kill();

    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        if matches!(child.try_wait(), Ok(Some(_))) {
            // A backend may exit before a transcoder that ignored SIGTERM.
            // Reaping only the leader would leave the remaining process group.
            #[cfg(target_os = "linux")]
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            return;
        }
        thread::sleep(Duration::from_millis(40));
    }
    #[cfg(target_os = "linux")]
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(target_os = "windows")]
pub struct Job(windows_sys::Win32::Foundation::HANDLE);

#[cfg(target_os = "windows")]
unsafe impl Send for Job {}

#[cfg(target_os = "windows")]
impl Job {
    pub fn attach(child: &Child) -> Result<Self, String> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return Err("无法创建后端进程管理对象".into());
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let configured = SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const std::ffi::c_void,
                std::mem::size_of_val(&info) as u32,
            );
            let assigned =
                configured != 0 && AssignProcessToJobObject(handle, child.as_raw_handle()) != 0;
            if !assigned {
                CloseHandle(handle);
                return Err("无法将后端加入进程管理对象，请检查系统策略".into());
            }
            Ok(Self(handle))
        }
    }
}

#[cfg(target_os = "windows")]
impl Drop for Job {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::process::Stdio;

    #[test]
    fn terminate_reaps_descendants_even_when_leader_exits_first() {
        let mut command = Command::new("sh");
        command
            .arg("-c")
            .arg("sh -c 'trap \"\" TERM; exec sleep 30' & echo $!; wait")
            .stdout(Stdio::piped());
        prepare(&mut command);
        let mut child = command.spawn().expect("spawn test process group");
        let mut line = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut line)
            .expect("read descendant pid");
        let descendant: u32 = line.trim().parse().expect("descendant pid");
        thread::sleep(Duration::from_millis(100));
        terminate(&mut child);
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let stat = std::fs::read_to_string(format!("/proc/{descendant}/stat"));
            // A reparented zombie is terminated and awaits the host's init.
            if stat.as_ref().is_err()
                || stat.as_ref().is_ok_and(|s| {
                    s.rsplit_once(") ")
                        .is_some_and(|(_, rest)| rest.starts_with('Z'))
                })
            {
                break;
            }
            assert!(Instant::now() < deadline, "descendant survived shutdown");
            thread::sleep(Duration::from_millis(20));
        }
    }
}
