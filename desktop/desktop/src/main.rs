#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Write},
    os::windows::{fs::OpenOptionsExt, process::CommandExt},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent};

struct Backend {
    child: Mutex<Child>,
    origin: tauri::Url,
    token: String,
    closed: AtomicBool,
    data: PathBuf,
    _instance: File,
}

fn show(app: &tauri::AppHandle) -> tauri::Result<()> {
    if let Some(window) = app.get_webview_window("main") {
        window.show()?;
        return window.set_focus();
    }
    let backend = app.state::<Backend>();
    backend.closed.store(false, Ordering::Release);
    let origin = backend.origin.clone();
    let expected = origin.origin();
    WebviewWindowBuilder::new(app, "main", WebviewUrl::External(origin.clone()))
        .data_directory(backend.data.join("webview"))
        .title("AutoScholar")
        .inner_size(1200.0, 820.0)
        .initialization_script(format!(
            "if(location.origin === '{}') Object.defineProperty(window,'__autoscholarSession',{{value:'{}'}})",
            origin.origin().ascii_serialization(), backend.token))
        .on_navigation(move |url| url.origin() == expected)
        .build()?;
    Ok(())
}

// The OS closes this job handle when the desktop process exits, including after a crash.
fn supervise_process_tree() -> std::io::Result<()> {
    use windows_sys::Win32::{
        Foundation::CloseHandle,
        System::{JobObjects::*, Threading::GetCurrentProcess},
    };
    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return Err(std::io::Error::last_os_error());
        }
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            std::mem::size_of_val(&limits) as u32,
        ) == 0
            || AssignProcessToJobObject(job, GetCurrentProcess()) == 0
        {
            let error = std::io::Error::last_os_error();
            CloseHandle(job);
            return Err(error);
        }
    }
    Ok(())
}

fn main() {
    let app = tauri::Builder::default()
        .setup(|app| {
            supervise_process_tree()?;
            let data = match std::env::var_os("AUTOSCHOLAR_DESKTOP_PROFILE") {
                Some(path) => std::path::absolute(path)?,
                None => {
                    let config = app.path().resource_dir()?.join("profile-path.txt");
                    match fs::read(config) {
                        Ok(bytes) => {
                            use std::os::windows::ffi::OsStringExt;
                            let mut pairs = bytes.chunks_exact(2);
                            let units: Vec<_> = pairs
                                .by_ref()
                                .map(|p| u16::from_le_bytes([p[0], p[1]]))
                                .collect();
                            let path = PathBuf::from(std::ffi::OsString::from_wide(&units));
                            if !pairs.remainder().is_empty() || !path.is_absolute() {
                                return Err("Invalid installed data directory".into());
                            }
                            path
                        }
                        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                            app.path().app_local_data_dir()?
                        }
                        Err(e) => return Err(e.into()),
                    }
                }
            };
            fs::create_dir_all(&data)?;
            let instance = match OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .share_mode(0)
                .open(data.join("instance.lock"))
            {
                Ok(file) => file,
                Err(e) if e.raw_os_error() == Some(32) => {
                    app.handle().exit(0);
                    return Ok(());
                }
                Err(e) => return Err(e.into()),
            };
            let runtime = app.path().resource_dir()?.join("runtime");
            let token: String = (0..4)
                .map(|_| format!("{:016x}", rand::random::<u64>()))
                .collect();
            let mut child = Command::new(runtime.join("python.exe"))
                .args(["-I", "-u", "-m", "autoscholar.desktop"])
                .arg(&data)
                .arg(runtime.join("web"))
                .current_dir(&data)
                .creation_flags(0x08000000)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit())
                .spawn()?;
            writeln!(child.stdin.as_mut().unwrap(), "{token}")?;
            let mut output = BufReader::new(child.stdout.take().unwrap());
            let mut line = String::new();
            let port = loop {
                line.clear();
                if output.read_line(&mut line)? == 0 {
                    return Err("Desktop backend failed to start".into());
                }
                if let Some(value) = line.trim().strip_prefix("READY ") {
                    break value.parse::<u16>()?;
                }
            };
            let origin = format!("http://127.0.0.1:{port}/").parse()?;
            app.manage(Backend {
                child: Mutex::new(child),
                origin,
                token,
                data,
                closed: AtomicBool::new(false),
                _instance: instance,
            });
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                for msg in output.lines().map_while(Result::ok) {
                    if msg == "IDLE"
                        && handle.state::<Backend>().closed.load(Ordering::Acquire)
                        && handle.get_webview_window("main").is_none()
                    {
                        handle.exit(0);
                    }
                }
                handle.exit(0);
            });
            let icon = app
                .default_window_icon()
                .ok_or("Missing application icon")?
                .clone();
            TrayIconBuilder::new()
                .icon(icon)
                .tooltip("AutoScholar")
                .on_tray_icon_event(|tray, event| {
                    if matches!(
                        event,
                        TrayIconEvent::Click {
                            button: MouseButton::Left,
                            button_state: MouseButtonState::Up,
                            ..
                        }
                    ) {
                        let _ = show(tray.app_handle());
                    }
                })
                .build(app)?;
            show(app.handle())?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                window
                    .state::<Backend>()
                    .closed
                    .store(true, Ordering::Release);
                let _ = window.destroy();
            }
        })
        .build(tauri::generate_context!())
        .expect("AutoScholar startup failed");
    app.run(|app, event| match event {
        tauri::RunEvent::ExitRequested {
            api, code: None, ..
        } => api.prevent_exit(),
        tauri::RunEvent::Exit => {
            if let Some(state) = app.try_state::<Backend>() {
                if let Ok(mut child) = state.child.lock() {
                    drop(child.stdin.take());
                    let _ = child.wait();
                }
            }
        }
        _ => (),
    });
}
