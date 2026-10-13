//! Private LAN HTTPS endpoint for phone MIC and mobile song/transport controls.
//! The desktop remains the sole authority for the queue and mpv playback.
use crate::{audio::MicState, library::{Library, Song}};
use anyhow::{Context, Result};
use axum::{
    extract::{ws::{Message, WebSocketUpgrade}, Query, State},
    http::{header, HeaderMap, StatusCode},
    response::{Html, IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use rcgen::{
    BasicConstraints, CertificateParams, DnType, ExtendedKeyUsagePurpose,
    IsCa, Issuer, KeyPair, KeyUsagePurpose, SanType,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs,
    net::{IpAddr, Ipv4Addr, SocketAddr},
    path::{Path, PathBuf},
    sync::{atomic::Ordering, Arc, RwLock},
    time::Duration,
};
use tokio::sync::{mpsc, oneshot};

const PORT_TLS: u16 = 8443;
const PORT_BOOT: u16 = 8080;

/// No local paths are included in responses returned to the browser.
#[derive(Clone, Default, Serialize)]
pub struct SongBrief {
    pub id: i64,
    pub title: String,
    pub format: String,
}
impl From<&Song> for SongBrief {
    fn from(song: &Song) -> Self {
        Self { id: song.id, title: song.title.clone(), format: song.format.clone() }
    }
}

#[derive(Clone, Default, Serialize)]
pub struct RemoteSnapshot {
    pub current: Option<SongBrief>,
    pub queue: Vec<SongBrief>,
    pub key_requested: i8,
    pub key_confirmed: i8,
    pub key_pending: bool,
    pub loaded: bool,
    pub paused: bool,
    pub status: String,
}

#[derive(Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum RemoteAction {
    AddSong { id: i64 },
    PlayNow { id: i64 },
    Next,
    SetKey { value: i8 },
}

pub struct RemoteRequest {
    pub action: RemoteAction,
    pub reply: oneshot::Sender<std::result::Result<(), String>>,
}

#[derive(Clone)]
struct WebState {
    mic: MicState,
    token: String,
    ca_der: Vec<u8>,
    library: Library,
    snapshot: Arc<RwLock<RemoteSnapshot>>,
    commands: mpsc::Sender<RemoteRequest>,
}

pub struct PhoneService {
    pub mic_url: String,
    pub setup_url: String,
    pub ca_path: PathBuf,
    pub ca_fingerprint: String,
    pub remote_rx: mpsc::Receiver<RemoteRequest>,
    snapshot: Arc<RwLock<RemoteSnapshot>>,
}
impl PhoneService {
    pub fn publish(&self, state: RemoteSnapshot) {
        *self.snapshot.write().unwrap_or_else(|e| e.into_inner()) = state;
    }
}

fn ensure_ca(path: &Path) -> Result<(String, Vec<u8>)> {
    fs::create_dir_all(path)?;
    let ca_file = path.join("openktv-ca.crt");
    let key_file = path.join("openktv-ca.key");
    let der_file = path.join("openktv-ca.cer");
    if !ca_file.exists() || !key_file.exists() || !der_file.exists() {
        // Keep existing CA when the computer's LAN IP changes.
        let mut p = CertificateParams::new(Vec::<String>::new())?;
        p.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        p.distinguished_name.push(DnType::CommonName, "OpenKTV Private Local CA");
        p.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
        p.not_after = rcgen::date_time_ymd(2040, 1, 1);
        let key = KeyPair::generate()?;
        let ca = p.self_signed(&key)?;
        fs::write(&ca_file, ca.pem())?;
        fs::write(&key_file, key.serialize_pem())?;
        fs::write(&der_file, ca.der())?;
    }
    Ok((fs::read_to_string(&ca_file)?, fs::read(&der_file)?))
}

fn server_certificate(ca_dir: &Path, ip: IpAddr) -> Result<(PathBuf, PathBuf, Vec<u8>)> {
    let (ca_pem, ca_der) = ensure_ca(ca_dir)?;
    let ca_key = KeyPair::from_pem(&fs::read_to_string(ca_dir.join("openktv-ca.key"))?)?;
    let ca_issuer = Issuer::from_ca_cert_pem(&ca_pem, ca_key)?;
    let mut p = CertificateParams::new(vec!["localhost".into()])?;
    p.distinguished_name.push(DnType::CommonName, "OpenKTV LAN Microphone and Remote");
    p.subject_alt_names.push(SanType::IpAddress(ip));
    p.extended_key_usages.push(ExtendedKeyUsagePurpose::ServerAuth);
    p.key_usages.push(KeyUsagePurpose::DigitalSignature);
    p.not_after = time::OffsetDateTime::now_utc() + time::Duration::days(360);
    let key = KeyPair::generate()?;
    let cert = p.signed_by(&key, &ca_issuer)?;
    let cert_file = ca_dir.join("server.crt");
    let key_file = ca_dir.join("server.key");
    fs::write(&cert_file, format!("{}\n{}", cert.pem(), ca_pem))?;
    fs::write(&key_file, key.serialize_pem())?;
    Ok((cert_file, key_file, ca_der))
}

async fn phone_page() -> impl IntoResponse {
    (
        [(header::CONTENT_TYPE, "text/html; charset=utf-8"),
         (header::CACHE_CONTROL, "no-store"),
         (header::REFERRER_POLICY, "no-referrer"),
         (header::X_CONTENT_TYPE_OPTIONS, "nosniff")],
        Html(include_str!("../assets/mic.html")),
    )
}
async fn ca_download(State(s): State<WebState>) -> impl IntoResponse {
    (
        [(header::CONTENT_TYPE, "application/pkix-cert"),
         (header::CONTENT_DISPOSITION, "attachment; filename=OpenKTV-Local-CA.cer")],
        s.ca_der,
    )
}
async fn bootstrap(State(s): State<WebState>) -> impl IntoResponse {
    let fingerprint = Sha256::digest(&s.ca_der).iter().map(|b| format!("{b:02X}")).collect::<Vec<_>>().join(":");
    Html(format!(r#"<!DOCTYPE html><html lang="zh-Hant"><meta name="viewport" content="width=device-width, initial-scale=1"><meta charset="utf-8"><title>OpenKTV 憑證設定</title><body style="font-family:system-ui;max-width:680px;margin:2em auto;padding:1em"><h2>OpenKTV 手機 MIC／點歌台：初次憑證設定</h2><p>請先在電腦的 OpenKTV 視窗核對以下 CA SHA256 指紋。核對一致後才能安裝並信任此憑證。</p><code style="overflow-wrap:anywhere">{fingerprint}</code><p><a href="/ca.cer">下載 OpenKTV 本機 CA 憑證</a></p><p>Android：設定 → 安全性／加密與憑證 → 安裝 CA 憑證（不同手機路徑略異）；iPhone：下載描述檔後前往設定安裝，再至 一般 → 關於本機 → 憑證信任設定 啟用。</p><p>完成後回到 Windows OpenKTV 視窗，掃描「HTTPS 手機遙控／MIC」QR Code。</p><strong>切勿將 OpenKTV 電腦內的 openktv-ca.key 分享給其他人；配對網址也不要分享給不信任的人。</strong></body></html>"#))
}

fn authorized(headers: &HeaderMap, token: &str) -> bool {
    headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok())
        .map(|v| v.strip_prefix("Bearer ") == Some(token)).unwrap_or(false)
}

async fn api_state(State(s): State<WebState>, headers: HeaderMap) -> Response {
    if !authorized(&headers, &s.token) { return StatusCode::UNAUTHORIZED.into_response(); }
    let current = s.snapshot.read().unwrap_or_else(|e| e.into_inner()).clone();
    ([(header::CACHE_CONTROL, "no-store")], Json(current)).into_response()
}

#[derive(Deserialize)]
struct SongQuery { q: Option<String> }
async fn api_songs(State(s): State<WebState>, headers: HeaderMap, Query(query): Query<SongQuery>) -> Response {
    if !authorized(&headers, &s.token) { return StatusCode::UNAUTHORIZED.into_response(); }
    let query = query.q.unwrap_or_default().chars().take(100).collect::<String>();
    match s.library.search(&query, 80) {
        Ok(songs) => {
            let songs: Vec<SongBrief> = songs.iter().map(SongBrief::from).collect();
            ([(header::CACHE_CONTROL, "no-store")], Json(json!({"songs": songs}))).into_response()
        }
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": format!("曲庫查詢失敗：{e}")}))).into_response(),
    }
}

async fn api_command(State(s): State<WebState>, headers: HeaderMap, Json(action): Json<RemoteAction>) -> Response {
    if !authorized(&headers, &s.token) { return StatusCode::UNAUTHORIZED.into_response(); }
    let (tx, rx) = oneshot::channel();
    if s.commands.try_send(RemoteRequest { action, reply: tx }).is_err() {
        return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error":"電腦控制台忙碌，請稍後再試"}))).into_response();
    }
    // The egui event loop owns the player. The HTTP handler never touches mpv directly.
    match tokio::time::timeout(Duration::from_secs(4), rx).await {
        Ok(Ok(Ok(()))) => (StatusCode::OK, Json(json!({"ok":true}))).into_response(),
        Ok(Ok(Err(message))) => (StatusCode::CONFLICT, Json(json!({"error": message}))).into_response(),
        _ => (StatusCode::GATEWAY_TIMEOUT, Json(json!({"error":"等待電腦控制台回應逾時"}))).into_response(),
    }
}

async fn mic_ws(ws: WebSocketUpgrade, Query(q): Query<std::collections::HashMap<String, String>>, State(s): State<WebState>) -> impl IntoResponse {
    let authorized = q.get("token").map(String::as_str) == Some(s.token.as_str());
    if !authorized { return StatusCode::UNAUTHORIZED.into_response(); }
    ws.max_message_size(8192).on_upgrade(move |mut socket| async move {
        if s.mic.connected.compare_exchange(0, 1, Ordering::SeqCst, Ordering::SeqCst).is_err() {
            let _ = socket.send(Message::Text("MIC_BUSY".into())).await;
            let _ = socket.send(Message::Close(None)).await;
            return;
        }
        s.mic.clear();
        while let Some(Ok(msg)) = socket.recv().await {
            match msg {
                Message::Binary(bytes) => s.mic.enqueue_pcm16(&bytes),
                Message::Close(_) => break,
                _ => {}
            }
        }
        s.mic.connected.store(0, Ordering::SeqCst);
        s.mic.muted.store(true, Ordering::SeqCst);
        s.mic.clear();
    }).into_response()
}

pub fn launch(mic: MicState, ca_dir: &Path, library: Library) -> Result<PhoneService> {
    let ip = local_ip_address::local_ip().context("無法判斷區域網路 IP，請先連接 Wi-Fi")?;
    if !matches!(ip, IpAddr::V4(_)) { anyhow::bail!("目前僅支援 IPv4 區域網路"); }
    let (cert_file, key_file, ca_der) = server_certificate(ca_dir, ip)?;
    let fingerprint = Sha256::digest(&ca_der).iter().map(|b| format!("{b:02X}")).collect::<Vec<_>>().join(":");
    let token = uuid::Uuid::new_v4().simple().to_string() + &uuid::Uuid::new_v4().simple().to_string();
    let mic_url = format!("https://{ip}:{PORT_TLS}/#token={token}");
    let setup_url = format!("http://{ip}:{PORT_BOOT}/");
    let (commands, remote_rx) = mpsc::channel(64);
    let snapshot = Arc::new(RwLock::new(RemoteSnapshot::default()));
    let state = WebState { mic, token, ca_der, library, snapshot: snapshot.clone(), commands };
    let bind_ip = IpAddr::V4(Ipv4Addr::UNSPECIFIED);
    let http_addr = SocketAddr::new(bind_ip, PORT_BOOT);
    let tls_addr = SocketAddr::new(bind_ip, PORT_TLS);
    let check_http = std::net::TcpListener::bind(http_addr).context("HTTP 8080 連接埠已被佔用")?;
    let check_tls = std::net::TcpListener::bind(tls_addr).context("HTTPS 8443 連接埠已被佔用")?;
    drop(check_http);
    drop(check_tls);
    std::thread::spawn(move || {
        let runtime = tokio::runtime::Builder::new_multi_thread().enable_all().build();
        match runtime {
            Ok(runtime) => runtime.block_on(async move {
                let tls = match axum_server::tls_rustls::RustlsConfig::from_pem_file(&cert_file, &key_file).await {
                    Ok(c) => c,
                    Err(e) => { eprintln!("OpenKTV TLS 啟動失敗: {e}"); return; }
                };
                let http_router = Router::new()
                    .route("/", get(bootstrap))
                    .route("/ca.cer", get(ca_download))
                    .with_state(state.clone());
                let https_router = Router::new()
                    .route("/", get(phone_page))
                    .route("/ws", get(mic_ws))
                    .route("/api/state", get(api_state))
                    .route("/api/songs", get(api_songs))
                    .route("/api/command", post(api_command))
                    .layer(axum::extract::DefaultBodyLimit::max(2048))
                    .with_state(state);
                tokio::spawn(async move {
                    match tokio::net::TcpListener::bind(http_addr).await {
                        Ok(listener) => { if let Err(e) = axum::serve(listener, http_router).await { eprintln!("CA bootstrap error: {e}"); } },
                        Err(e) => eprintln!("CA bootstrap bind error: {e}"),
                    }
                });
                if let Err(e) = axum_server::bind_rustls(tls_addr, tls).serve(https_router.into_make_service()).await {
                    eprintln!("HTTPS Server error: {e}");
                }
            }),
            Err(e) => eprintln!("Tokio runtime 啟動失敗: {e}"),
        }
    });
    Ok(PhoneService { mic_url, setup_url, ca_path: ca_dir.join("openktv-ca.cer"), ca_fingerprint: fingerprint, remote_rx, snapshot })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ca_remains_the_same() {
        let dir = std::env::temp_dir().join(format!("openktv-ca-{}", uuid::Uuid::new_v4()));
        let (_, a) = ensure_ca(&dir).unwrap();
        let (_, b) = ensure_ca(&dir).unwrap();
        assert_eq!(a, b);
        let _ = std::fs::remove_dir_all(dir);
    }
    #[test]
    fn brief_does_not_expose_file_path() {
        let song = Song { id: 1, title: "Test".into(), path: "private/example.mp4".into(), format: "dual".into() };
        let text = serde_json::to_string(&SongBrief::from(&song)).unwrap();
        assert!(!text.contains("private"));
        assert!(text.contains("Test"));
    }
}
