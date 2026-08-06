// Veya Desktop — Tauri 壳 (Rust)
//
// 职责: 启动时自动拉起 veya Python 后端 (L4 gateway), 健康检查就绪后
//       由 Tauri 自动加载静态前端 (frontendDist = 网页版 adapter-static 构建),
//       退出时终止后端子进程。与网页版同一套前后端代码 → 能力完全对齐。
//
// 后端定位顺序:
//   1. <exe>/resources/backend/veya-backend   (PyInstaller 打包, 下载即用)
//   2. <repo>/venv/bin/python -m veya.server.app (开发模式)
//   3. python3 -m veya.server.app            (系统环境, veya 已安装)
//
// 环境变量: VEYA_BACKEND_PORT (默认 8767), VEYA_DESKTOP_LOG (打印子进程日志)

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, State};

struct BackendHandle(Mutex<Option<Child>>);

fn default_port() -> u16 {
    std::env::var("VEYA_BACKEND_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8767)
}

/// 定位后端命令 (返回 (cmd, args, label))
fn find_backend() -> (String, Vec<String>, String) {
    // 1. 打包产物: resources/backend/veya-backend
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let bundled = dir.join("resources").join("backend").join("veya-backend");
            if bundled.exists() {
                return (bundled.to_string_lossy().into_owned(), vec![], "bundled".into());
            }
        }
    }
    // 2. 开发模式: 仓库 venv
    let repo_venv = {
        let mut p = std::env::current_dir().unwrap_or_default();
        p.push("venv");
        p.push("bin");
        p.push("python");
        p
    };
    if repo_venv.exists() {
        return (
            repo_venv.to_string_lossy().into_owned(),
            vec!["-m".into(), "veya.server.app".into()],
            "venv".into(),
        );
    }
    // 3. 系统 python
    ("python3".into(), vec!["-m".into(), "veya.server.app".into()], "system".into())
}

/// 等待后端健康检查就绪
fn wait_healthy(port: u16, timeout_secs: u64) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{port}/api/v1/mcp/health");
    let deadline = std::time::Instant::now() + Duration::from_secs(timeout_secs);
    while std::time::Instant::now() < deadline {
        if let Ok(resp) = reqwest::blocking::get(&url) {
            if resp.status().is_success() {
                return Ok(());
            }
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    Err(format!("后端启动超时 ({timeout_secs}s): {url}"))
}

fn spawn_backend(port: u16, label: &str) -> Result<Child, String> {
    let (cmd, mut args, _) = find_backend();
    args.push("--host".into());
    args.push("127.0.0.1".into());
    args.push("--port".into());
    args.push(port.to_string());
    println!("[veya-desktop] 后端: {cmd} {args:?} ({label})");

    let mut child = Command::new(&cmd)
        .args(&args)
        .stdout(if std::env::var("VEYA_DESKTOP_LOG").is_ok() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stderr(if std::env::var("VEYA_DESKTOP_LOG").is_ok() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .spawn()
        .map_err(|e| format!("启动后端失败: {e}"))?;

    if std::env::var("VEYA_DESKTOP_LOG").is_ok() {
        if let Some(out) = child.stdout.take() {
            std::thread::spawn(move || {
                for line in BufReader::new(out).lines().map_while(|l| l.ok()) {
                    println!("[backend] {line}");
                }
            });
        }
        if let Some(err) = child.stderr.take() {
            std::thread::spawn(move || {
                for line in BufReader::new(err).lines().map_while(|l| l.ok()) {
                    eprintln!("[backend] {line}");
                }
            });
        }
    }
    Ok(child)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let port = default_port();

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(move |app| {
            let (_, _, label) = find_backend();
            match spawn_backend(port, &label) {
                Ok(child) => {
                    app.manage(BackendHandle(Mutex::new(Some(child))));
                }
                Err(e) => {
                    eprintln!("[veya-desktop] {e}");
                    // 后端不可用也继续: 窗口可打开, UI 会显示连接错误
                    app.manage(BackendHandle(Mutex::new(None)));
                }
            }
            // 健康检查: 失败只告警不退出 (后端可能由外部启动)
            match wait_healthy(port, 90) {
                Ok(()) => println!("[veya-desktop] 后端就绪 :{port}"),
                Err(e) => eprintln!("[veya-desktop] 警告: {e}"),
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                // 窗口关闭 → 终止后端
                if let Some(state) = window.try_state::<BackendHandle>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(child) = guard.take() {
                            let _ = kill_child(child);
                        }
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![backend_port])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[tauri::command]
fn backend_port(state: State<BackendHandle>) -> u16 {
    let _ = state;
    default_port()
}

fn kill_child(mut child: Child) -> std::io::Result<()> {
    let _ = child.kill();
    let _ = child.wait();
    Ok(())
}
