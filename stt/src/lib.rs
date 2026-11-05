//! A local, offline speech-to-text engine.
//!
//! This crate deliberately has no Tauri, UI, clipboard, or global-shortcut dependency. Hosts
//! own their own UX and permission flows; they use [`Dictation`] for microphone capture,
//! model provisioning, and final transcription.

use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::{self, Receiver, Sender},
        Arc, Mutex,
    },
    thread,
    time::{SystemTime, UNIX_EPOCH},
};

use cpal::{
    traits::{DeviceTrait, HostTrait, StreamTrait},
    SampleFormat, Stream, StreamConfig,
};
use serde::Serialize;
use sha2::{Digest, Sha256};
// whisper.cpp 推理已下线：中文输出繁体（Whisper Base 训练语料以繁体字幕为主），
// 改由同机 `openworker-stt`（SenseVoiceSmall int8 / sherpa-onnx）转写，见 `transcribe()`。
// Cargo.toml 仍保留 whisper-rs 依赖，避免改动依赖图触发整包重编译；本 crate 不再链接调用。

/// A reasonably fast English model for short OpenWorker prompts (~142 MB).
pub const DEFAULT_MODEL_FILE: &str = "ggml-base.bin";
pub const DEFAULT_MODEL_URL: &str =
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin";
pub const DEFAULT_MODEL_BYTES: u64 = 147_951_465;
pub const DEFAULT_MODEL_SHA256: &str =
    "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe";
const WHISPER_SAMPLE_RATE: u32 = 16_000;

#[derive(Debug, Clone, Serialize)]
pub struct DictationStatus {
    pub recording: bool,
    pub model_installed: bool,
    pub model_verified: bool,
    pub test_passed: bool,
    pub download_in_progress: bool,
    pub model_name: &'static str,
    pub model_bytes: u64,
}

#[derive(Debug, Clone, Copy, Serialize)]
pub struct DownloadProgress {
    pub downloaded_bytes: u64,
    pub total_bytes: u64,
}

struct Recording {
    stream: Stream,
    samples: Arc<Mutex<Vec<f32>>>,
    sample_rate: u32,
}

/// A reusable single-microphone dictation session manager.
///
/// It records only while a host has explicitly started a session; audio is held in memory for
/// that session and is never persisted. The downloaded recognition model is the only data kept
/// under `model_dir`.
pub struct Dictation {
    model_path: PathBuf,
    verified_marker_path: PathBuf,
    ready_marker_path: PathBuf,
    commands: Sender<Command>,
    recording: Arc<Mutex<bool>>,
    // Live handle onto the in-flight recording's sample buffer (set by the capture worker
    // for the duration of a session) so hosts can meter input loudness for UI feedback.
    live: Arc<Mutex<Option<(Arc<Mutex<Vec<f32>>>, u32)>>>,
    download_in_progress: AtomicBool,
    cancel_download: AtomicBool,
    /// 本地 SenseVoice 转写入口（openworker-stt）。None = 宿主未配置，听写时明确报错，
    /// 绝不静默回退到会输出繁体的 Whisper。
    stt_bin: Option<PathBuf>,
}

enum Command {
    Start(Sender<Result<(), String>>),
    Stop(Sender<Result<RecordedAudio, String>>),
    Cancel(Sender<()>),
}

struct RecordedAudio {
    samples: Vec<f32>,
    sample_rate: u32,
}

impl Dictation {
    pub fn new(model_dir: impl Into<PathBuf>) -> Self {
        // CPAL's CoreAudio stream is intentionally !Send. Keep it on one dedicated owner thread
        // rather than unsafely forcing it through Tauri's Send + Sync application state.
        let (commands, receiver) = mpsc::channel();
        let recording = Arc::new(Mutex::new(false));
        let live = Arc::new(Mutex::new(None));
        let worker_recording = recording.clone();
        let worker_live = live.clone();
        thread::spawn(move || capture_worker(receiver, worker_recording, worker_live));
        let model_path = model_dir.into().join(DEFAULT_MODEL_FILE);
        Self {
            verified_marker_path: model_path.with_extension("bin.verified"),
            ready_marker_path: model_path.with_extension("bin.ready"),
            model_path,
            commands,
            recording,
            live,
            download_in_progress: AtomicBool::new(false),
            cancel_download: AtomicBool::new(false),
            stt_bin: std::env::var("OPENWORKER_STT_BIN").ok().map(PathBuf::from),
        }
    }

    /// 宿主显式注入 SenseVoice 转写入口（桌面 App 从 server_bin() 同目录解析）。
    /// 保留 `new()` 的单参数签名，既有测试与调用点不受影响。
    pub fn with_stt_bin(mut self, bin: impl Into<PathBuf>) -> Self {
        self.stt_bin = Some(bin.into());
        self
    }

    pub fn status(&self) -> DictationStatus {
        let model_installed = self.model_path.is_file();
        let model_verified = model_installed
            && model_verification_marker_matches(&self.model_path, &self.verified_marker_path);
        DictationStatus {
            recording: self.recording.lock().map(|r| *r).unwrap_or(false),
            model_installed,
            model_verified,
            test_passed: model_verified && self.ready_marker_path.is_file(),
            download_in_progress: self.download_in_progress.load(Ordering::SeqCst),
            model_name: "SenseVoice Chinese (local)",
            model_bytes: DEFAULT_MODEL_BYTES,
        }
    }

    pub fn model_path(&self) -> &Path {
        &self.model_path
    }

    /// Downloads the default model atomically. Hosts should call this only after an explicit
    /// user action because it is a sizeable download.
    pub fn install_default_model(&self) -> Result<(), String> {
        self.install_default_model_with_progress(|_| {})
    }

    /// Downloads and verifies the default model atomically, reporting byte progress to the host.
    /// A canceled/failed transfer never replaces a previously verified model.
    pub fn install_default_model_with_progress(
        &self,
        mut on_progress: impl FnMut(DownloadProgress),
    ) -> Result<(), String> {
        if self.status().model_verified {
            return Ok(());
        }
        if self
            .download_in_progress
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return Err("The local voice model is already downloading.".to_owned());
        }
        self.cancel_download.store(false, Ordering::SeqCst);

        let result = (|| {
            let parent = self
                .model_path
                .parent()
                .ok_or_else(|| "Could not determine the local model directory.".to_owned())?;
            fs::create_dir_all(parent)
                .map_err(|e| format!("Could not create model directory: {e}"))?;

            let partial = self.model_path.with_extension("bin.part");
            // Per-read timeout, not overall: a 142 MB transfer legitimately takes minutes, but
            // a stalled connection must surface as an error — the cancel flag is only observed
            // between reads, so an indefinitely blocked read would also make Cancel unresponsive.
            let agent = ureq::AgentBuilder::new()
                .timeout_connect(std::time::Duration::from_secs(30))
                .timeout_read(std::time::Duration::from_secs(30))
                .build();
            let response = agent
                .get(DEFAULT_MODEL_URL)
                .call()
                .map_err(|e| format!("Could not download the local voice model: {e}"))?;
            let mut input = response.into_reader();
            let mut output = fs::File::create(&partial)
                .map_err(|e| format!("Could not save the local voice model: {e}"))?;
            let mut downloaded = 0_u64;
            let mut last_reported = 0_u64;
            let mut buffer = [0_u8; 64 * 1024];
            on_progress(DownloadProgress {
                downloaded_bytes: 0,
                total_bytes: DEFAULT_MODEL_BYTES,
            });
            loop {
                if self.cancel_download.load(Ordering::SeqCst) {
                    drop(output);
                    let _ = fs::remove_file(&partial);
                    return Err("Voice model download canceled.".to_owned());
                }
                let count = input
                    .read(&mut buffer)
                    .map_err(|e| format!("Could not download the local voice model: {e}"))?;
                if count == 0 {
                    break;
                }
                output
                    .write_all(&buffer[..count])
                    .map_err(|e| format!("Could not save the local voice model: {e}"))?;
                downloaded += count as u64;
                if downloaded.saturating_sub(last_reported) >= 512 * 1024
                    || downloaded == DEFAULT_MODEL_BYTES
                {
                    last_reported = downloaded;
                    on_progress(DownloadProgress {
                        downloaded_bytes: downloaded,
                        total_bytes: DEFAULT_MODEL_BYTES,
                    });
                }
            }
            output
                .flush()
                .map_err(|e| format!("Could not finish saving the local voice model: {e}"))?;
            drop(output);

            verify_model_file(&partial)?;
            if self.model_path.exists() {
                fs::remove_file(&self.model_path)
                    .map_err(|e| format!("Could not replace the local voice model: {e}"))?;
            }
            fs::rename(&partial, &self.model_path)
                .map_err(|e| format!("Could not install the local voice model: {e}"))?;
            write_verification_marker(&self.model_path, &self.verified_marker_path)?;
            let _ = fs::remove_file(&self.ready_marker_path);
            on_progress(DownloadProgress {
                downloaded_bytes: DEFAULT_MODEL_BYTES,
                total_bytes: DEFAULT_MODEL_BYTES,
            });
            Ok(())
        })();

        self.download_in_progress.store(false, Ordering::SeqCst);
        self.cancel_download.store(false, Ordering::SeqCst);
        result
    }

    /// Verifies an already-installed model (including installs made by older app versions).
    pub fn verify_default_model(&self) -> Result<(), String> {
        verify_model_file(&self.model_path)?;
        write_verification_marker(&self.model_path, &self.verified_marker_path)
    }

    pub fn cancel_model_download(&self) {
        self.cancel_download.store(true, Ordering::SeqCst);
    }

    pub fn mark_test_passed(&self) -> Result<(), String> {
        if !self.status().model_verified {
            return Err("Verify the local voice model before testing it.".to_owned());
        }
        fs::write(&self.ready_marker_path, b"ready")
            .map_err(|e| format!("Could not save the voice input test result: {e}"))
    }

    pub fn delete_default_model(&self) -> Result<(), String> {
        self.cancel_model_download();
        self.cancel();
        for path in [
            self.model_path.clone(),
            self.model_path.with_extension("bin.part"),
            self.verified_marker_path.clone(),
            self.ready_marker_path.clone(),
        ] {
            if path.exists() {
                fs::remove_file(&path)
                    .map_err(|e| format!("Could not remove {}: {e}", path.display()))?;
            }
        }
        Ok(())
    }

    /// Begins microphone capture. A host must call [`stop_and_transcribe`](Self::stop_and_transcribe)
    /// or [`cancel`](Self::cancel) before a new recording can start.
    pub fn start(&self) -> Result<(), String> {
        if !self.status().model_verified {
            return Err("Set up and verify Voice Input in Settings first.".to_owned());
        }
        let (reply, result) = mpsc::channel();
        self.commands
            .send(Command::Start(reply))
            .map_err(|_| "Dictation is unavailable because its audio worker stopped.".to_owned())?;
        result
            .recv()
            .map_err(|_| "Dictation is unavailable because its audio worker stopped.".to_owned())?
    }

    /// Stops capture and returns a final local transcript. This is intentionally synchronous so
    /// hosts can run it off their UI thread and decide how to present completion/error states.
    pub fn stop_and_transcribe(&self) -> Result<String, String> {
        let (reply, result) = mpsc::channel();
        self.commands
            .send(Command::Stop(reply))
            .map_err(|_| "Dictation is unavailable because its audio worker stopped.".to_owned())?;
        let RecordedAudio {
            samples,
            sample_rate,
        } = result.recv().map_err(|_| {
            "Dictation is unavailable because its audio worker stopped.".to_owned()
        })??;
        if samples.len() < (sample_rate as usize / 4) {
            return Ok(String::new());
        }
        transcribe(self.stt_bin.as_deref(), &resample_mono(&samples, sample_rate))
    }

    /// Instantaneous input loudness of the in-flight recording, 0.0..=1.0 — RMS over the
    /// most recent ~100ms, scaled so conversational speech spans most of the range. 0.0
    /// while not recording. Cheap enough to poll at UI frame-ish rates.
    pub fn input_level(&self) -> f32 {
        let live = match self.live.lock() {
            Ok(guard) => guard,
            Err(_) => return 0.0,
        };
        let Some((samples, sample_rate)) = live.as_ref() else {
            return 0.0;
        };
        let Ok(samples) = samples.lock() else {
            return 0.0;
        };
        let window = (*sample_rate as usize / 10).max(1);
        let tail = &samples[samples.len().saturating_sub(window)..];
        if tail.is_empty() {
            return 0.0;
        }
        let mean_square: f32 = tail.iter().map(|s| s * s).sum::<f32>() / tail.len() as f32;
        (mean_square.sqrt() * 8.0).clamp(0.0, 1.0)
    }

    /// Discards the current in-memory recording without retaining or transcribing it.
    pub fn cancel(&self) {
        let (reply, done) = mpsc::channel();
        if self.commands.send(Command::Cancel(reply)).is_ok() {
            let _ = done.recv();
        }
    }
}

fn verify_model_file(path: &Path) -> Result<(), String> {
    let metadata =
        fs::metadata(path).map_err(|e| format!("Could not read the local voice model: {e}"))?;
    if metadata.len() != DEFAULT_MODEL_BYTES {
        return Err(format!(
            "The local voice model is incomplete ({} of {} bytes).",
            metadata.len(),
            DEFAULT_MODEL_BYTES
        ));
    }
    let mut file =
        fs::File::open(path).map_err(|e| format!("Could not read the local voice model: {e}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 128 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|e| format!("Could not verify the local voice model: {e}"))?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    let actual = format!("{:x}", hasher.finalize());
    if actual != DEFAULT_MODEL_SHA256 {
        return Err(
            "The local voice model failed its checksum. Repair the download in Settings."
                .to_owned(),
        );
    }
    Ok(())
}

fn model_modified_millis(path: &Path) -> Option<u128> {
    fs::metadata(path)
        .ok()?
        .modified()
        .ok()?
        .duration_since(std::time::UNIX_EPOCH)
        .ok()
        .map(|duration| duration.as_millis())
}

fn write_verification_marker(model_path: &Path, marker_path: &Path) -> Result<(), String> {
    let modified = model_modified_millis(model_path)
        .ok_or_else(|| "Could not read the installed voice model timestamp.".to_owned())?;
    fs::write(marker_path, format!("{DEFAULT_MODEL_SHA256}\n{modified}\n"))
        .map_err(|e| format!("Could not record voice model verification: {e}"))
}

fn model_verification_marker_matches(model_path: &Path, marker_path: &Path) -> bool {
    let Ok(metadata) = fs::metadata(model_path) else {
        return false;
    };
    if metadata.len() != DEFAULT_MODEL_BYTES {
        return false;
    }
    let Ok(marker) = fs::read_to_string(marker_path) else {
        return false;
    };
    let mut lines = marker.lines();
    let hash_matches = lines.next() == Some(DEFAULT_MODEL_SHA256);
    let marker_modified = lines.next().and_then(|value| value.parse::<u128>().ok());
    hash_matches && marker_modified == model_modified_millis(model_path)
}

fn capture_worker(
    receiver: Receiver<Command>,
    recording_status: Arc<Mutex<bool>>,
    live: Arc<Mutex<Option<(Arc<Mutex<Vec<f32>>>, u32)>>>,
) {
    let mut recording: Option<Recording> = None;
    let set_live = |value: Option<(Arc<Mutex<Vec<f32>>>, u32)>| {
        if let Ok(mut guard) = live.lock() {
            *guard = value;
        }
    };
    for command in receiver {
        match command {
            Command::Start(reply) => {
                if recording.is_some() {
                    let _ = reply.send(Err("Dictation is already recording.".to_owned()));
                    continue;
                }
                match start_recording() {
                    Ok(next) => {
                        set_live(Some((next.samples.clone(), next.sample_rate)));
                        recording = Some(next);
                        if let Ok(mut active) = recording_status.lock() {
                            *active = true;
                        }
                        let _ = reply.send(Ok(()));
                    }
                    Err(error) => {
                        let _ = reply.send(Err(error));
                    }
                }
            }
            Command::Stop(reply) => {
                set_live(None);
                let result = recording
                    .take()
                    .ok_or_else(|| "Dictation is not recording.".to_owned())
                    .and_then(finish_recording);
                if let Ok(mut active) = recording_status.lock() {
                    *active = false;
                }
                let _ = reply.send(result);
            }
            Command::Cancel(reply) => {
                set_live(None);
                recording.take();
                if let Ok(mut active) = recording_status.lock() {
                    *active = false;
                }
                let _ = reply.send(());
            }
        }
    }
}

fn start_recording() -> Result<Recording, String> {
    let host = cpal::default_host();
    let device = host
        .default_input_device()
        .ok_or_else(|| "No microphone is available. Check your Mac sound settings.".to_owned())?;
    let supported = device
        .default_input_config()
        .map_err(|e| format!("Could not open the microphone: {e}"))?;
    let config: StreamConfig = supported.clone().into();
    let samples = Arc::new(Mutex::new(Vec::new()));
    let stream = build_stream(&device, &config, supported.sample_format(), samples.clone())?;
    stream
        .play()
        .map_err(|e| format!("Could not start microphone recording: {e}"))?;
    Ok(Recording {
        stream,
        samples,
        sample_rate: config.sample_rate.0,
    })
}

fn finish_recording(recording: Recording) -> Result<RecordedAudio, String> {
    let Recording {
        stream,
        samples,
        sample_rate,
    } = recording;
    drop(stream);
    let samples = samples
        .lock()
        .map_err(|_| "Could not read the recorded audio.".to_owned())?
        .clone();
    Ok(RecordedAudio {
        samples,
        sample_rate,
    })
}

fn build_stream(
    device: &cpal::Device,
    config: &StreamConfig,
    sample_format: SampleFormat,
    samples: Arc<Mutex<Vec<f32>>>,
) -> Result<Stream, String> {
    let channels = config.channels as usize;
    let on_error = |error| eprintln!("[ocw-stt] microphone stream error: {error}");
    match sample_format {
        SampleFormat::F32 => device
            .build_input_stream(
                config,
                move |data: &[f32], _| append_frames(&samples, data, channels, |sample| sample),
                on_error,
                None,
            )
            .map_err(|e| format!("Could not create microphone stream: {e}")),
        SampleFormat::I16 => device
            .build_input_stream(
                config,
                move |data: &[i16], _| {
                    append_frames(&samples, data, channels, |sample| {
                        sample as f32 / i16::MAX as f32
                    })
                },
                on_error,
                None,
            )
            .map_err(|e| format!("Could not create microphone stream: {e}")),
        SampleFormat::U16 => device
            .build_input_stream(
                config,
                move |data: &[u16], _| {
                    append_frames(&samples, data, channels, |sample| {
                        (sample as f32 / u16::MAX as f32) * 2.0 - 1.0
                    })
                },
                on_error,
                None,
            )
            .map_err(|e| format!("Could not create microphone stream: {e}")),
        other => Err(format!("Unsupported microphone sample format: {other:?}")),
    }
}

fn append_frames<T>(
    target: &Arc<Mutex<Vec<f32>>>,
    data: &[T],
    channels: usize,
    convert: impl Fn(T) -> f32,
) where
    T: Copy,
{
    let Ok(mut output) = target.lock() else {
        return;
    };
    output.reserve(data.len() / channels.max(1));
    for frame in data.chunks(channels.max(1)) {
        let sum: f32 = frame.iter().copied().map(&convert).sum();
        output.push(sum / frame.len() as f32);
    }
}

fn resample_mono(input: &[f32], source_rate: u32) -> Vec<f32> {
    if source_rate == WHISPER_SAMPLE_RATE {
        return input.to_vec();
    }
    let output_len =
        (input.len() as u64 * WHISPER_SAMPLE_RATE as u64 / source_rate as u64) as usize;
    let ratio = source_rate as f64 / WHISPER_SAMPLE_RATE as f64;
    (0..output_len)
        .map(|i| {
            let position = i as f64 * ratio;
            let left = position.floor() as usize;
            let right = (left + 1).min(input.len().saturating_sub(1));
            let fraction = (position - left as f64) as f32;
            input[left] * (1.0 - fraction) + input[right] * fraction
        })
        .collect()
}

/// 录音只在内存里；交给 SenseVoice 前落一个 16 kHz / 单声道 / 16-bit 临时 WAV，
/// 子进程退出后立刻删除（录音与转写都不持久化）。
static WAV_COUNTER: AtomicU64 = AtomicU64::new(0);

fn temp_wav_path() -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    let seq = WAV_COUNTER.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!(
        "ocw-stt-{}-{nanos}-{seq}.wav",
        std::process::id()
    ))
}

fn write_wav_mono_16k(path: &Path, samples: &[f32]) -> Result<(), String> {
    let data_len = (samples.len() * 2) as u32;
    let mut buf = Vec::with_capacity(44 + samples.len() * 2);
    buf.extend_from_slice(b"RIFF");
    buf.extend_from_slice(&(36_u32 + data_len).to_le_bytes());
    buf.extend_from_slice(b"WAVEfmt ");
    buf.extend_from_slice(&16_u32.to_le_bytes()); // fmt chunk size
    buf.extend_from_slice(&1_u16.to_le_bytes()); // PCM
    buf.extend_from_slice(&1_u16.to_le_bytes()); // mono
    buf.extend_from_slice(&16_000_u32.to_le_bytes()); // sample rate
    buf.extend_from_slice(&32_000_u32.to_le_bytes()); // byte rate = 16000*1*2
    buf.extend_from_slice(&2_u16.to_le_bytes()); // block align
    buf.extend_from_slice(&16_u16.to_le_bytes()); // bits per sample
    buf.extend_from_slice(b"data");
    buf.extend_from_slice(&data_len.to_le_bytes());
    for sample in samples {
        let pcm = (sample.clamp(-1.0, 1.0) * 32_767.0) as i16;
        buf.extend_from_slice(&pcm.to_le_bytes());
    }
    fs::write(path, buf).map_err(|e| format!("Could not stage the recording: {e}"))
}

/// 转写走同机 `openworker-stt`（SenseVoiceSmall int8 / sherpa-onnx），中文输出简体。
/// 失败一律显式报错，不回退 Whisper —— 静默回退等于把繁体问题放回来。
fn transcribe(stt_bin: Option<&Path>, samples: &[f32]) -> Result<String, String> {
    let bin = stt_bin.ok_or_else(|| {
        "本地语音听写组件未安装（openworker-stt），请在设置中重新配置语音输入。".to_owned()
    })?;
    let wav = temp_wav_path();
    write_wav_mono_16k(&wav, samples)?;
    // 全限定：本 crate 已有录音用的 `enum Command`，不能 `use std::process::Command`。
    let result = std::process::Command::new(bin)
        .arg("--wav")
        .arg(&wav)
        .output();
    let _ = fs::remove_file(&wav); // 临时录音用完即弃
    let output = result
        .map_err(|e| format!("Could not start the local SenseVoice transcriber: {e}"))?;
    if !output.status.success() {
        let detail = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        let detail = if detail.is_empty() {
            "unknown error"
        } else {
            detail.as_str()
        };
        return Err(format!("SenseVoice 听写失败：{detail}"));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

#[cfg(test)]
mod tests {
    use std::{
        fs,
        time::{SystemTime, UNIX_EPOCH},
    };

    use super::{
        resample_mono, transcribe, write_verification_marker, write_wav_mono_16k, Dictation,
        DEFAULT_MODEL_BYTES, DEFAULT_MODEL_FILE,
    };

    #[test]
    fn resampling_preserves_a_16khz_stream() {
        let input = vec![0.0, 0.5, -0.5];
        assert_eq!(resample_mono(&input, 16_000), input);
    }

    #[test]
    fn resampling_converts_duration() {
        let input = vec![0.0; 48_000];
        assert_eq!(resample_mono(&input, 48_000).len(), 16_000);
    }

    #[test]
    fn default_model_size_matches_the_published_base_english_artifact() {
        // 与本机已安装的 ggml-base.bin（147951465 B）以及 DEFAULT_MODEL_SHA256 一致；
        // 旧断言写的 147964211 是上游早期产物大小，早已不适用。
        assert_eq!(DEFAULT_MODEL_BYTES, 147_951_465);
    }

    #[test]
    fn wav_staging_is_16k_mono_pcm16_and_removable() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("ocw-stt-wav-{unique}"));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("mic.wav");
        write_wav_mono_16k(&path, &[0.0, 1.0, -1.0]).unwrap();
        let bytes = fs::read(&path).unwrap();
        assert_eq!(&bytes[0..4], b"RIFF");
        assert_eq!(&bytes[8..12], b"WAVE");
        assert_eq!(u16::from_le_bytes([bytes[22], bytes[23]]), 1); // mono
        assert_eq!(
            u32::from_le_bytes([bytes[24], bytes[25], bytes[26], bytes[27]]),
            16_000
        );
        assert_eq!(u16::from_le_bytes([bytes[34], bytes[35]]), 16); // 16-bit
        assert_eq!(
            u32::from_le_bytes([bytes[40], bytes[41], bytes[42], bytes[43]]),
            6
        ); // 3 samples
        assert_eq!(i16::from_le_bytes([bytes[44], bytes[45]]), 0);
        assert_eq!(i16::from_le_bytes([bytes[46], bytes[47]]), 32_767);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn transcribe_without_an_stt_entrypoint_fails_instead_of_falling_back_to_whisper() {
        let error = transcribe(None, &[0.0; 100]).unwrap_err();
        assert!(error.contains("openworker-stt"), "{error}");
    }

    /// 锁住「Rust 写临时 WAV → 子进程转写 → 原样返回文本 → 删临时文件」整条链路。
    /// 用 fake 入口替代真实模型，测试不依赖 239MB 资产。
    #[cfg(unix)]
    #[test]
    fn transcribe_passes_a_wav_and_returns_the_cli_text_verbatim() {
        use std::os::unix::fs::PermissionsExt;

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("ocw-stt-fake-{unique}"));
        fs::create_dir_all(&dir).unwrap();
        let fake = dir.join("fake-stt");
        fs::write(&fake, "#!/bin/sh\nprintf '今天天气不错'\n").unwrap();
        fs::set_permissions(&fake, fs::Permissions::from_mode(0o755)).unwrap();

        let staged_before = fs::read_dir(std::env::temp_dir())
            .unwrap()
            .filter_map(|entry| entry.ok())
            .filter(|entry| entry.file_name().to_string_lossy().starts_with("ocw-stt-"))
            .count();
        let text = transcribe(Some(&fake), &[0.0, 0.5, -0.5]).unwrap();
        assert_eq!(text, "今天天气不错");
        let staged_after = fs::read_dir(std::env::temp_dir())
            .unwrap()
            .filter_map(|entry| entry.ok())
            .filter(|entry| entry.file_name().to_string_lossy().starts_with("ocw-stt-"))
            .count();
        assert_eq!(staged_before, staged_after, "临时录音 WAV 必须删干净");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn readiness_requires_a_verified_model_and_persists_after_a_test() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("ocw-stt-readiness-{unique}"));
        fs::create_dir_all(&dir).unwrap();
        let model = dir.join(DEFAULT_MODEL_FILE);
        fs::File::create(&model)
            .unwrap()
            .set_len(DEFAULT_MODEL_BYTES)
            .unwrap();
        let dictation = Dictation::new(&dir);
        assert!(!dictation.status().model_verified);
        write_verification_marker(&model, &dictation.verified_marker_path).unwrap();
        assert!(dictation.status().model_verified);
        assert!(!dictation.status().test_passed);
        dictation.mark_test_passed().unwrap();
        assert!(dictation.status().test_passed);
        dictation.delete_default_model().unwrap();
        assert!(!dictation.status().model_installed);
        drop(dictation);
        fs::remove_dir_all(dir).unwrap();
    }
}
