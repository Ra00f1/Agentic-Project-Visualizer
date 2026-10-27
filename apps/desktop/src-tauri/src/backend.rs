//! Spawns and manages the Python backend sidecar (task 20).
//!
//! Debug builds run it through `uv` from the monorepo's `apps/backend`
//! directory, unless `APV_DEV_BACKEND_URL`/`APV_DEV_SESSION_TOKEN` are
//! both set — that's the manual mode for `AGENTIC_SESSION_TOKEN=<tok> uv
//! run uvicorn agentic_visualizer.main:app --port 8765 --reload`, started
//! by hand in a second terminal. Release builds run the bundled
//! executable (Task 21 puts it at `<resource_dir>/backend/`); release
//! builds never honor `APV_DEV_*`, so nothing in the user's own
//! environment can redirect the installed app at a different backend.
//!
//! The spawn happens on a background thread (flagged per CLAUDE.md §12):
//! `setup()` must return quickly, and reading the handshake line blocks.
//! `BackendState` is a condvar-guarded `Option` so `backend_info()` (an
//! async Tauri command, possibly called more than once) can wait for
//! that thread's result without polling.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

const HANDSHAKE_PREFIX: &str = "APV_BACKEND_READY ";
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(30);
const SHUTDOWN_GRACE: Duration = Duration::from_secs(3);
/// How many trailing stderr lines to surface in a failure message —
/// enough to show the real Python traceback without dumping an
/// unbounded log into the error panel.
const STDERR_TAIL_LINES: usize = 20;

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// What the frontend gets back: enough to build every request/WS URL
/// and attach the bearer token. `camelCase` on the wire, matching every
/// other model this app serializes to TypeScript.
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BackendInfo {
    pub base_url: String,
    pub token: String,
}

/// The JSON object `serve.py` prints after `APV_BACKEND_READY `. Only
/// the fields this side actually needs — `version`/`pid` are read by a
/// human from the log, not by this struct, so they're dropped instead of
/// carried through a field that would otherwise never be read, which
/// would be dead code the moment an unused-field lint noticed it.
#[derive(Deserialize)]
struct Handshake {
    port: u16,
    token: String,
}

enum HandshakeOutcome {
    Ready(BackendInfo),
    Failed(String),
}

/// Managed app state: the handshake outcome (set once, read many times),
/// plus the child process and its stdin handle for shutdown.
pub struct BackendState {
    outcome: Mutex<Option<HandshakeOutcome>>,
    condvar: Condvar,
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<ChildStdin>>,
}

impl BackendState {
    fn new() -> Self {
        Self {
            outcome: Mutex::new(None),
            condvar: Condvar::new(),
            child: Mutex::new(None),
            stdin: Mutex::new(None),
        }
    }

    /// Set once; a second call (there shouldn't be one) is a no-op
    /// rather than overwriting an already-delivered result.
    fn set_outcome(&self, outcome: HandshakeOutcome) {
        let mut guard = self.outcome.lock().expect("backend state mutex poisoned");
        if guard.is_none() {
            *guard = Some(outcome);
            self.condvar.notify_all();
        }
    }

    /// Waits up to `HANDSHAKE_TIMEOUT` for the handshake. Safe to call
    /// more than once (a frontend retry after a transient error) — it
    /// always returns the SAME outcome once one exists, never spawns a
    /// second backend.
    pub fn wait_for_info(&self) -> Result<BackendInfo, String> {
        let guard = self.outcome.lock().expect("backend state mutex poisoned");
        let (guard, timed_out) = self
            .condvar
            .wait_timeout_while(guard, HANDSHAKE_TIMEOUT, |outcome| outcome.is_none())
            .expect("backend state mutex poisoned");
        match &*guard {
            Some(HandshakeOutcome::Ready(info)) => Ok(info.clone()),
            Some(HandshakeOutcome::Failed(message)) => Err(message.clone()),
            None => {
                debug_assert!(timed_out.timed_out());
                Err(format!(
                    "backend did not start within {}s",
                    HANDSHAKE_TIMEOUT.as_secs()
                ))
            }
        }
    }

    /// `RunEvent::Exit`: drop stdin (its EOF is `serve.py`'s own cue to
    /// shut down gracefully), wait briefly, then kill if it's still up.
    pub fn shutdown(&self) {
        drop(self.stdin.lock().expect("backend state mutex poisoned").take());
        let mut child_guard = self.child.lock().expect("backend state mutex poisoned");
        let Some(child) = child_guard.as_mut() else {
            return;
        };
        let deadline = std::time::Instant::now() + SHUTDOWN_GRACE;
        loop {
            match child.try_wait() {
                Ok(Some(_status)) => return,
                Ok(None) => {
                    if std::time::Instant::now() >= deadline {
                        let _ = child.kill();
                        let _ = child.wait();
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
                Err(_) => return,
            }
        }
    }
}

/// Starts the background spawn/handshake thread and returns the state
/// the rest of the app (the `backend_info` command, and shutdown)
/// coordinates through. Called once from `setup()`.
pub fn spawn_backend(app: &AppHandle) -> Arc<BackendState> {
    let state = Arc::new(BackendState::new());
    let worker_state = state.clone();
    let worker_app = app.clone();
    std::thread::spawn(move || {
        if let Err(message) = run_backend(&worker_app, &worker_state) {
            worker_state.set_outcome(HandshakeOutcome::Failed(message));
        }
    });
    state
}

fn run_backend(app: &AppHandle, state: &BackendState) -> Result<(), String> {
    // Manual dev mode: both env vars set means "don't spawn anything,
    // the developer already started the backend by hand." Debug-only —
    // a release build ignores these even if somehow set, so nothing in
    // the installed app's environment can redirect it.
    #[cfg(debug_assertions)]
    if let (Ok(base_url), Ok(token)) = (
        std::env::var("APV_DEV_BACKEND_URL"),
        std::env::var("APV_DEV_SESSION_TOKEN"),
    ) {
        state.set_outcome(HandshakeOutcome::Ready(BackendInfo { base_url, token }));
        return Ok(());
    }

    let mut command = build_command()?;
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = command
        .spawn()
        .map_err(|err| format!("failed to spawn backend: {err}"))?;
    let stdout = child.stdout.take().expect("stdout was piped");
    let stderr = child.stderr.take().expect("stderr was piped");
    let stdin = child.stdin.take().expect("stdin was piped");
    *state.stdin.lock().expect("backend state mutex poisoned") = Some(stdin);
    // Stored immediately, not after the handshake arrives: `shutdown()`
    // must be able to kill this child even if the app exits (or is
    // killed) while the backend is still starting up -- e.g. a cold `uv`
    // cache taking longer than the 30s handshake timeout. Storing it only
    // in the handshake-success branch left `state.child` `None` for that
    // entire window, so `shutdown()` silently no-op'd and the process was
    // orphaned.
    *state.child.lock().expect("backend state mutex poisoned") = Some(child);

    let log_path = rotate_and_open_log(app)?;
    let stderr_tail: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));

    // stderr: every line goes to the log file AND into a bounded tail
    // buffer, so a startup failure's error message can show the actual
    // Python traceback, not just "it didn't start."
    {
        let log_path = log_path.clone();
        let stderr_tail = stderr_tail.clone();
        std::thread::spawn(move || {
            let mut log_file = open_log_for_append(&log_path);
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                if let Some(file) = log_file.as_mut() {
                    let _ = writeln!(file, "{line}");
                }
                let mut tail = stderr_tail.lock().expect("stderr tail mutex poisoned");
                tail.push(line);
                if tail.len() > STDERR_TAIL_LINES {
                    tail.remove(0);
                }
            }
        });
    }

    // stdout: the FIRST line starting with the handshake prefix is
    // parsed as the handshake; everything else (before or after) goes
    // to the log. The handshake line itself is never written there —
    // see `rotate_and_open_log`'s own doc comment on why that matters.
    let mut log_file = open_log_for_append(&log_path);
    let mut handshake_seen = false;
    for line in BufReader::new(stdout).lines().map_while(Result::ok) {
        if !handshake_seen {
            match parse_handshake_line(&line) {
                Some(Ok(handshake)) => {
                    handshake_seen = true;
                    if let Some(file) = log_file.as_mut() {
                        let _ = writeln!(file, "APV_BACKEND_READY port={}", handshake.port);
                    }
                    state.set_outcome(HandshakeOutcome::Ready(BackendInfo {
                        base_url: format!("http://127.0.0.1:{}", handshake.port),
                        token: handshake.token,
                    }));
                    continue;
                }
                Some(Err(err)) => {
                    return Err(format!("malformed handshake line: {err}"));
                }
                None => {}
            }
        }
        if let Some(file) = log_file.as_mut() {
            let _ = writeln!(file, "{line}");
        }
    }

    // stdout closed. If we never saw a handshake, the process either
    // exited or crashed before becoming ready -- report the stderr
    // tail so the error panel shows the real failure, not just silence.
    if !handshake_seen {
        let tail = stderr_tail.lock().expect("stderr tail mutex poisoned").join("\n");
        return Err(format!(
            "backend exited before it was ready. Last output:\n{tail}\nLog: {}",
            log_path.display()
        ));
    }
    Ok(())
}

/// `None` when `line` isn't a handshake line at all (ordinary output,
/// written to the log elsewhere). `Some(Err(...))` for a
/// handshake-prefixed line that fails to parse — malformed JSON or a
/// field missing, both `serde_json::Error` since `Handshake` has no
/// optional/defaulted fields to tell the two apart separately. Pulled
/// out of `run_backend`'s IO loop specifically so it's unit-testable
/// without spawning a process — see the `tests` module below.
fn parse_handshake_line(line: &str) -> Option<Result<Handshake, serde_json::Error>> {
    line.strip_prefix(HANDSHAKE_PREFIX)
        .map(serde_json::from_str::<Handshake>)
}

#[cfg(debug_assertions)]
fn build_command() -> Result<Command, String> {
    let backend_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../backend");
    let mut command = Command::new("uv");
    command
        .arg("run")
        .arg("--project")
        .arg(&backend_dir)
        .arg("python")
        .arg("-m")
        .arg("agentic_visualizer.serve")
        .arg("--watch-stdin");
    Ok(command)
}

#[cfg(not(debug_assertions))]
fn build_command() -> Result<Command, String> {
    // Task 21 places the frozen backend here. Until then, fail with a
    // clear message instead of silently falling back to anything else.
    let exe_name = if cfg!(target_os = "windows") {
        "apv-backend.exe"
    } else {
        "apv-backend"
    };
    let path = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join("backend").join(exe_name)))
        .ok_or_else(|| "could not determine the app's own directory".to_string())?;
    if !path.is_file() {
        return Err(format!("bundled backend not found at {}", path.display()));
    }
    let mut command = Command::new(&path);
    command.arg("--watch-stdin");
    Ok(command)
}

/// Renames an existing `backend.log` to `backend.prev.log` (overwriting
/// any older `backend.prev.log`), so each launch's log is readable on
/// its own without last run's output mixed in, while still keeping one
/// prior run around for "it worked yesterday, what changed" debugging.
/// Returns the path to append this run's output to.
///
/// Never writes the raw handshake line (which contains the token) --
/// every write site in `run_backend` above writes either an ordinary
/// stdout/stderr line, or the redacted `APV_BACKEND_READY port=<port>`
/// substitute for the handshake specifically.
fn rotate_and_open_log(app: &AppHandle) -> Result<std::path::PathBuf, String> {
    let log_dir = app
        .path()
        .app_log_dir()
        .map_err(|err| format!("could not resolve the app log directory: {err}"))?;
    std::fs::create_dir_all(&log_dir).map_err(|err| format!("could not create {}: {err}", log_dir.display()))?;
    let log_path = log_dir.join("backend.log");
    let prev_path = log_dir.join("backend.prev.log");
    if log_path.is_file() {
        let _ = std::fs::rename(&log_path, &prev_path);
    }
    Ok(log_path)
}

fn open_log_for_append(path: &std::path::Path) -> Option<std::fs::File> {
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .ok()
}

/// `#[tauri::command]` -- waits for the handshake (bounded by the same
/// 30s timeout `wait_for_info` uses) and returns it, or the error.
#[tauri::command]
pub async fn backend_info(state: tauri::State<'_, Arc<BackendState>>) -> Result<BackendInfo, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.wait_for_info())
        .await
        .map_err(|err| format!("backend_info task panicked: {err}"))?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_valid_handshake_line() {
        let line = r#"APV_BACKEND_READY {"port": 54321, "token": "abc123", "version": "0.1.0", "pid": 1234}"#;
        let handshake = parse_handshake_line(line).expect("should be a handshake line").expect("should parse");
        assert_eq!(handshake.port, 54321);
        assert_eq!(handshake.token, "abc123");
    }

    #[test]
    fn ignores_ordinary_stdout_noise() {
        assert!(parse_handshake_line("INFO:     Uvicorn running on http://127.0.0.1:54321").is_none());
        assert!(parse_handshake_line("").is_none());
    }

    #[test]
    fn malformed_json_after_the_prefix_is_an_error() {
        let line = "APV_BACKEND_READY {not valid json";
        assert!(parse_handshake_line(line).expect("is a handshake line").is_err());
    }

    #[test]
    fn missing_required_field_is_an_error() {
        let line = r#"APV_BACKEND_READY {"port": 54321}"#; // no "token"
        assert!(parse_handshake_line(line).expect("is a handshake line").is_err());
    }
}
