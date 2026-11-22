//! Engine-independent logic for the Unified Agent desktop shell (slice S0).
//!
//! The shell is a window over the Mac hub's `/ui`. Everything that decides
//! *where* that window may go lives here, free of Tauri, so it can be tested
//! on any machine and reused if the webview engine is swapped (the Chromium
//! fallback in decision 3):
//!
//! - which hub to open ([`resolve_hub`]),
//! - how a `unified-agent://` link becomes a `/ui?view=` URL ([`parse_deep_link`]),
//! - which navigations stay in the window, open in the browser, or are refused
//!   ([`classify_navigation`]),
//! - what a launch or a forwarded second launch asked for ([`parse_args`]).

use std::fmt;

use url::Url;

/// Hub used when no flag, environment variable or config file names one.
pub const DEFAULT_HUB_URL: &str = "http://127.0.0.1:8765";
/// Environment variable that overrides the config file.
pub const HUB_ENV: &str = "UA_DESKTOP_HUB_URL";
/// Deep-link scheme (DESIGN D11, provisional until #1617 closes).
pub const SCHEME: &str = "unified-agent";
/// Bundled page shown when the hub cannot be reached.
pub const OFFLINE_PAGE: &str = "offline.html";

const MAX_PARAMS: usize = 16;
const MAX_VALUE_LEN: usize = 512;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// The configured hub URL is not a bare `http(s)://host[:port]` origin.
    Hub(String),
    /// The config file is not the JSON shape we expect.
    Config(String),
    /// A `unified-agent://` link we refuse to follow.
    DeepLink(String),
    /// Command-line usage error.
    Usage(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Hub(m) => write!(f, "hub URL: {m}"),
            Error::Config(m) => write!(f, "config file: {m}"),
            Error::DeepLink(m) => write!(f, "deep link: {m}"),
            Error::Usage(m) => write!(f, "{m}"),
        }
    }
}

impl std::error::Error for Error {}

/// A validated hub origin. Only the scheme, host and port are kept.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Hub {
    origin: Url,
}

impl Hub {
    /// Accept `http(s)://host[:port]` with an optional trailing `/`, or the
    /// same with `/ui` (people paste the page they have open). Anything else,
    /// such as credentials, a query or another path, is refused rather than
    /// silently dropped: a hub URL is security-relevant configuration.
    pub fn parse(raw: &str) -> Result<Self, Error> {
        let raw = raw.trim();
        let url = Url::parse(raw).map_err(|e| Error::Hub(format!("{raw:?} is not a URL ({e})")))?;
        if !matches!(url.scheme(), "http" | "https") {
            return Err(Error::Hub(format!("{raw:?} must use http or https")));
        }
        if url.host_str().is_none_or(str::is_empty) {
            return Err(Error::Hub(format!("{raw:?} has no host")));
        }
        if !url.username().is_empty() || url.password().is_some() {
            return Err(Error::Hub(
                "credentials do not belong in the hub URL".into(),
            ));
        }
        if url.query().is_some() || url.fragment().is_some() {
            return Err(Error::Hub(format!(
                "{raw:?} must not carry a query or fragment"
            )));
        }
        if !matches!(url.path(), "" | "/" | "/ui" | "/ui/") {
            return Err(Error::Hub(format!(
                "{raw:?} must be the hub origin, not a page under it"
            )));
        }
        let mut origin = url;
        origin.set_path("/");
        Ok(Self { origin })
    }

    /// `scheme://host:port` with no trailing slash, for display and for the
    /// offline page.
    pub fn origin(&self) -> String {
        self.origin.origin().ascii_serialization()
    }

    pub fn health_url(&self) -> Url {
        self.origin.join("/healthz").expect("static path joins")
    }

    /// `host:port` for a reachability probe.
    pub fn socket_addr(&self) -> String {
        let host = self.origin.host_str().expect("validated in parse");
        let port = self
            .origin
            .port_or_known_default()
            .expect("http(s) has a default port");
        format!("{host}:{port}")
    }

    /// The `/ui` page, optionally opened on a view.
    pub fn ui_url(&self, target: Option<&ViewTarget>) -> Url {
        let mut url = self.origin.join("/ui").expect("static path joins");
        if let Some(t) = target {
            let mut q = url.query_pairs_mut();
            q.append_pair("view", &t.view);
            for (k, v) in &t.params {
                q.append_pair(k, v);
            }
        }
        url
    }

    pub fn is_same_origin(&self, url: &Url) -> bool {
        url.origin() == self.origin.origin()
    }
}

/// Pick the hub: `--hub`, then `$UA_DESKTOP_HUB_URL`, then the config file,
/// then [`DEFAULT_HUB_URL`]. An invalid value at a higher level is an error,
/// never a silent fall-through to a lower one.
pub fn resolve_hub(
    cli: Option<&str>,
    env: Option<&str>,
    config_json: Option<&str>,
) -> Result<Hub, Error> {
    if let Some(v) = cli {
        return Hub::parse(v);
    }
    if let Some(v) = env.filter(|v| !v.trim().is_empty()) {
        return Hub::parse(v);
    }
    if let Some(text) = config_json {
        if let Some(v) = parse_config(text)?.hub_url {
            return Hub::parse(&v);
        }
    }
    Hub::parse(DEFAULT_HUB_URL)
}

/// `config.json` in the platform config directory.
#[derive(Debug, Default, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub hub_url: Option<String>,
}

pub fn parse_config(text: &str) -> Result<Config, Error> {
    serde_json::from_str(text).map_err(|e| Error::Config(e.to_string()))
}

/// A `/ui` view plus its deep-link parameters.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ViewTarget {
    pub view: String,
    pub params: Vec<(String, String)>,
}

/// Map `unified-agent://<view>?k=v` to a [`ViewTarget`].
///
/// S0 maps only the host to `?view=` and passes the query through. Object
/// paths such as `unified-agent://task/1556` arrive with the router in S2, so
/// a path is refused here rather than opened on the wrong page.
pub fn parse_deep_link(raw: &str) -> Result<ViewTarget, Error> {
    let url = Url::parse(raw.trim()).map_err(|e| Error::DeepLink(format!("not a URL ({e})")))?;
    if url.scheme() != SCHEME {
        return Err(Error::DeepLink(format!("scheme must be {SCHEME}://")));
    }
    let view = url.host_str().unwrap_or_default().to_ascii_lowercase();
    if !is_identifier(&view, 64, b'-') {
        return Err(Error::DeepLink(format!("{view:?} is not a view name")));
    }
    if !matches!(url.path(), "" | "/") {
        return Err(Error::DeepLink(format!(
            "object paths ({}) are not supported until the S2 router",
            url.path()
        )));
    }
    if !url.username().is_empty() || url.password().is_some() || url.port().is_some() {
        return Err(Error::DeepLink("unexpected credentials or port".into()));
    }
    let mut params = Vec::new();
    for (k, v) in url.query_pairs() {
        if k == "view" || !is_identifier(&k, 32, b'_') {
            return Err(Error::DeepLink(format!("parameter {k:?} is not allowed")));
        }
        if v.len() > MAX_VALUE_LEN {
            return Err(Error::DeepLink(format!("parameter {k:?} is too long")));
        }
        params.push((k.into_owned(), v.into_owned()));
        if params.len() > MAX_PARAMS {
            return Err(Error::DeepLink("too many parameters".into()));
        }
    }
    Ok(ViewTarget { view, params })
}

/// `[a-z][a-z0-9<sep>]*`, at most `max` bytes.
fn is_identifier(s: &str, max: usize, sep: u8) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= max
        && b[0].is_ascii_lowercase()
        && b.iter()
            .all(|&c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == sep)
}

/// What the window does with a navigation request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Nav {
    /// The hub, or a page bundled with the app: load it in the window.
    InApp,
    /// An ordinary web or mail link: hand it to the system browser.
    External,
    /// Anything else (`file:`, `javascript:`, `data:`, other schemes): refuse.
    Block,
}

pub fn classify_navigation(hub: &Hub, url: &Url) -> Nav {
    if is_bundle_origin(url) {
        // Only the offline page; never "anything bundled" (see is_offline_page).
        return if is_offline_page(url) {
            Nav::InApp
        } else {
            Nav::Block
        };
    }
    if hub.is_same_origin(url) || url.as_str() == "about:blank" {
        return Nav::InApp;
    }
    match url.scheme() {
        "http" | "https" | "mailto" => Nav::External,
        _ => Nav::Block,
    }
}

/// The bundled offline page, the only bundled page the window may navigate
/// to. Tauri serves bundled assets from `tauri://localhost` (Linux, macOS)
/// and `http(s)://tauri.localhost` (Windows). Matching the exact path, not
/// the whole bundle origin, keeps a future privileged bundled window (the S1
/// settings window) from becoming reachable through this window's policy.
pub fn is_offline_page(url: &Url) -> bool {
    is_bundle_origin(url) && url.path() == format!("/{OFFLINE_PAGE}")
}

fn is_bundle_origin(url: &Url) -> bool {
    let host = match url.scheme() {
        "tauri" => "localhost",
        "http" | "https" => "tauri.localhost",
        _ => return false,
    };
    url.host_str() == Some(host) && url.port().is_none()
}

/// Path and query, relative to the bundle root, of the "hub unreachable" page.
pub fn offline_page(hub: &Hub, next: &Url, reason: &str) -> String {
    let mut u = Url::parse("tauri://localhost/").expect("static URL parses");
    u.set_path(OFFLINE_PAGE);
    u.query_pairs_mut()
        .append_pair("hub", &hub.origin())
        .append_pair("next", next.as_str())
        .append_pair("reason", reason);
    format!("{}?{}", OFFLINE_PAGE, u.query().unwrap_or_default())
}

/// What a launch asked for. A second launch forwards its argv to the running
/// instance, which parses it with the same function.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct LaunchArgs {
    pub toggle: bool,
    pub help: bool,
    pub register_scheme: bool,
    pub hub: Option<String>,
    pub deep_link: Option<String>,
}

pub const USAGE: &str = "\
Usage: unified-agent-desktop [--hub URL] [--toggle] [--register-scheme] [unified-agent://VIEW?k=v]

  --hub URL          Hub origin (default: $UA_DESKTOP_HUB_URL, then config.json, then http://127.0.0.1:8765)
  --toggle           Show the window if hidden, hide it if shown (for a Hyprland bind)
  --register-scheme  Register this binary as the unified-agent:// handler, then exit (Linux)
  -h, --help         Show this help";

/// Parse argv without the program name.
pub fn parse_args<I, S>(args: I) -> Result<LaunchArgs, Error>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut out = LaunchArgs::default();
    let mut it = args.into_iter();
    while let Some(arg) = it.next() {
        let arg = arg.as_ref();
        match arg {
            "--toggle" => out.toggle = true,
            "-h" | "--help" => out.help = true,
            "--register-scheme" => out.register_scheme = true,
            "--hub" => {
                let v = it
                    .next()
                    .ok_or_else(|| Error::Usage("--hub needs a URL".into()))?;
                out.hub = Some(v.as_ref().to_owned());
            }
            _ if arg.starts_with("--hub=") => out.hub = Some(arg["--hub=".len()..].to_owned()),
            _ if arg.starts_with(&format!("{SCHEME}:")) => out.deep_link = Some(arg.to_owned()),
            _ => return Err(Error::Usage(format!("unknown argument {arg:?}"))),
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests;
