use serde::Serialize;
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, UNIX_EPOCH};

#[cfg(unix)]
use std::os::unix::net::UnixStream;

#[derive(Debug, Clone)]
struct Options {
    socket: PathBuf,
    interval: Duration,
    watch: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
struct EventFrame<'a> {
    source: &'static str,
    kind: &'a str,
    severity: &'static str,
    payload: serde_json::Value,
    dedup: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct FileStamp {
    len: u64,
    modified_ns: u128,
    is_directory: bool,
}

fn parse_args<I>(args: I) -> Result<Options, String>
where
    I: IntoIterator<Item = String>,
{
    let mut args = args.into_iter();
    let mut options = Options {
        socket: PathBuf::from("data/run/sentinel.sock"),
        interval: Duration::from_secs(5),
        watch: None,
    };
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--socket" => {
                options.socket = PathBuf::from(args.next().ok_or("--socket exige um caminho")?);
            }
            "--interval-secs" => {
                let value = args.next().ok_or("--interval-secs exige um número")?;
                let secs: u64 = value.parse().map_err(|_| "intervalo inválido")?;
                if !(1..=3600).contains(&secs) {
                    return Err("intervalo deve ficar entre 1 e 3600 segundos".into());
                }
                options.interval = Duration::from_secs(secs);
            }
            "--watch" => {
                options.watch = Some(PathBuf::from(
                    args.next().ok_or("--watch exige um diretório")?,
                ));
            }
            "--help" | "-h" => {
                return Err(
                    "uso: nexus-sentinel [--socket PATH] [--interval-secs N] [--watch DIR]".into(),
                );
            }
            other => return Err(format!("opção desconhecida: {other}")),
        }
    }
    Ok(options)
}

fn parse_meminfo(input: &str) -> (Option<u64>, Option<u64>) {
    let mut total = None;
    let mut available = None;
    for line in input.lines() {
        let mut fields = line.split_whitespace();
        let name = fields.next().unwrap_or_default();
        let value = fields.next().and_then(|value| value.parse::<u64>().ok());
        match name {
            "MemTotal:" => total = value,
            "MemAvailable:" => available = value,
            _ => {}
        }
    }
    (total, available)
}

fn proc_sample() -> serde_json::Value {
    let uptime = fs::read_to_string("/proc/uptime")
        .ok()
        .and_then(|value| value.split_whitespace().next()?.parse::<f64>().ok());
    let load = fs::read_to_string("/proc/loadavg").ok().and_then(|value| {
        let values: Option<Vec<f64>> = value
            .split_whitespace()
            .take(3)
            .map(|part| part.parse().ok())
            .collect();
        values
    });
    let memory = fs::read_to_string("/proc/meminfo")
        .ok()
        .map(|contents| parse_meminfo(&contents));
    let process_count = fs::read_dir("/proc").ok().map(|entries| {
        entries
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().parse::<u32>().is_ok())
            .count()
    });
    let cpu_model = fs::read_to_string("/proc/cpuinfo")
        .ok()
        .and_then(|contents| {
            contents.lines().find_map(|line| {
                let (key, value) = line.split_once(':')?;
                if matches!(key.trim(), "model name" | "Hardware") {
                    Some(value.trim().to_owned())
                } else {
                    None
                }
            })
        });
    let top_processes = fs::read_dir("/proc").ok().map(|entries| {
        let mut processes: Vec<(u32, String, u64)> = entries
            .filter_map(Result::ok)
            .filter_map(|entry| {
                let pid = entry.file_name().to_string_lossy().parse::<u32>().ok()?;
                let status = fs::read_to_string(entry.path().join("status")).ok()?;
                let name = status
                    .lines()
                    .find_map(|line| line.strip_prefix("Name:").map(str::trim))?
                    .to_owned();
                let rss = status.lines().find_map(|line| {
                    line.strip_prefix("VmRSS:")
                        .and_then(|value| value.split_whitespace().next())
                        .and_then(|value| value.parse::<u64>().ok())
                })?;
                Some((pid, name, rss))
            })
            .collect();
        processes.sort_by(|left, right| right.2.cmp(&left.2));
        processes
            .into_iter()
            .take(5)
            .map(|(pid, name, rss_kib)| serde_json::json!({"pid": pid, "name": name, "rss_kib": rss_kib}))
            .collect::<Vec<_>>()
    });
    let mut payload = serde_json::Map::new();
    if let Some(value) = uptime {
        payload.insert("uptime_s".into(), serde_json::json!(value));
    }
    if let Some(values) = load {
        payload.insert("load_1_5_15".into(), serde_json::json!(values));
    }
    if let Some((Some(total), available)) = memory {
        payload.insert("memory_total_kib".into(), serde_json::json!(total));
        if let Some(value) = available {
            payload.insert("memory_available_kib".into(), serde_json::json!(value));
        }
    }
    if let Some(value) = process_count {
        payload.insert("process_count".into(), serde_json::json!(value));
    }
    if let Some(value) = cpu_model {
        payload.insert("cpu_model".into(), serde_json::json!(value));
    }
    if let Some(value) = std::thread::available_parallelism().ok() {
        payload.insert("logical_cpu_count".into(), serde_json::json!(value.get()));
    }
    if let Some(value) = fs::read_to_string("/proc/sys/kernel/osrelease").ok() {
        payload.insert("kernel_release".into(), serde_json::json!(value.trim()));
    }
    if let Some(value) = top_processes {
        payload.insert("top_processes_by_rss".into(), serde_json::json!(value));
    }
    serde_json::Value::Object(payload)
}

fn snapshot(directory: &Path) -> std::io::Result<BTreeMap<PathBuf, FileStamp>> {
    let mut result = BTreeMap::new();
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        // Do not follow symlinks: the observer is intentionally shallow and read-only.
        if file_type.is_symlink() {
            continue;
        }
        let metadata = entry.metadata()?;
        let modified_ns = metadata
            .modified()
            .unwrap_or(UNIX_EPOCH)
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        result.insert(
            entry.path(),
            FileStamp {
                len: metadata.len(),
                modified_ns,
                is_directory: file_type.is_dir(),
            },
        );
    }
    Ok(result)
}

fn send_event(
    socket: &Path,
    kind: &str,
    payload: serde_json::Value,
    dedup: bool,
) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        let mut stream = UnixStream::connect(socket)?;
        stream.set_read_timeout(Some(Duration::from_secs(2)))?;
        let frame = EventFrame {
            source: "sentinel_rs",
            kind,
            severity: "info",
            payload,
            dedup,
        };
        serde_json::to_writer(&mut stream, &frame).map_err(std::io::Error::other)?;
        stream.write_all(b"\n")?;
        stream.flush()?;
        let mut response = String::new();
        BufReader::new(stream).read_line(&mut response)?;
        let ack: serde_json::Value =
            serde_json::from_str(&response).map_err(std::io::Error::other)?;
        if ack.get("ok").and_then(serde_json::Value::as_bool) == Some(true) {
            Ok(())
        } else {
            Err(std::io::Error::other("NEXUS recusou evento do sentinel"))
        }
    }
    #[cfg(not(unix))]
    {
        let _ = (socket, kind, payload, dedup);
        Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "Unix socket requer Linux/Unix",
        ))
    }
}

fn emit_watch_diff(
    socket: &Path,
    previous: &BTreeMap<PathBuf, FileStamp>,
    current: &BTreeMap<PathBuf, FileStamp>,
) {
    for (path, stamp) in current {
        let old = previous.get(path);
        let kind = match old {
            None => Some("perception.fs.created"),
            Some(old) if old != stamp => Some("perception.fs.modified"),
            _ => None,
        };
        if let Some(kind) = kind {
            let payload = serde_json::json!({
                "path": path.to_string_lossy(),
                "size_bytes": stamp.len,
                "is_directory": stamp.is_directory,
            });
            if let Err(error) = send_event(socket, kind, payload, false) {
                eprintln!("sentinel: falha ao registrar evento: {error}");
            }
        }
    }
    for (path, stamp) in previous {
        if !current.contains_key(path) {
            let payload = serde_json::json!({
                "path": path.to_string_lossy(),
                "size_bytes": stamp.len,
                "is_directory": stamp.is_directory,
            });
            if let Err(error) = send_event(socket, "perception.fs.deleted", payload, false) {
                eprintln!("sentinel: falha ao registrar evento: {error}");
            }
        }
    }
}

fn run(options: Options) -> Result<(), String> {
    if let Some(directory) = &options.watch {
        if !directory.is_dir() {
            return Err(format!(
                "diretório de observação não encontrado: {}",
                directory.display()
            ));
        }
    }
    let mut previous = options
        .watch
        .as_ref()
        .and_then(|directory| snapshot(directory).ok());
    loop {
        if let Err(error) = send_event(
            &options.socket,
            "perception.system.sample",
            proc_sample(),
            true,
        ) {
            eprintln!(
                "sentinel: NEXUS indisponível em {}: {error}",
                options.socket.display()
            );
        }
        if let Some(directory) = &options.watch {
            match snapshot(directory) {
                Ok(current) => {
                    if let Some(previous_snapshot) = &previous {
                        emit_watch_diff(&options.socket, previous_snapshot, &current);
                    }
                    previous = Some(current);
                }
                Err(error) => eprintln!(
                    "sentinel: não foi possível observar {}: {error}",
                    directory.display()
                ),
            }
        }
        thread::sleep(options.interval);
    }
}

fn main() {
    let options = match parse_args(env::args().skip(1)) {
        Ok(options) => options,
        Err(message) => {
            eprintln!("{message}");
            std::process::exit(2);
        }
    };
    if let Err(error) = run(options) {
        eprintln!("sentinel: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_memory_values_in_kib() {
        assert_eq!(
            parse_meminfo("MemTotal: 4096 kB\nMemAvailable: 1024 kB\n"),
            (Some(4096), Some(1024))
        );
    }

    #[test]
    fn rejects_zero_or_excessive_interval() {
        assert!(parse_args(["--interval-secs".into(), "0".into()]).is_err());
        assert!(parse_args(["--interval-secs".into(), "3601".into()]).is_err());
    }

    #[test]
    fn accepts_optional_watch_directory() {
        let options = parse_args(["--watch".into(), "/tmp".into()]).unwrap();
        assert_eq!(options.watch, Some(PathBuf::from("/tmp")));
    }
}
