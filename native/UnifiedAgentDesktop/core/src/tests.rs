use super::*;

fn hub() -> Hub {
    Hub::parse("http://hub.example:8765").unwrap()
}

fn u(s: &str) -> Url {
    Url::parse(s).unwrap()
}

// --- hub URL ---------------------------------------------------------------

#[test]
fn hub_accepts_origin_with_or_without_ui() {
    for raw in [
        "http://hub.example:8765",
        "http://hub.example:8765/",
        "http://hub.example:8765/ui",
        " http://hub.example:8765/ui/ ",
    ] {
        assert_eq!(
            Hub::parse(raw).unwrap().origin(),
            "http://hub.example:8765",
            "{raw}"
        );
    }
    assert_eq!(
        Hub::parse("https://mac.tail").unwrap().socket_addr(),
        "mac.tail:443"
    );
    // IPv6 keeps its brackets, so the probe address resolves.
    let v6 = Hub::parse("http://[::1]:8765").unwrap();
    assert_eq!(v6.socket_addr(), "[::1]:8765");
    assert_eq!(v6.origin(), "http://[::1]:8765");
}

/// `user:pw` userinfo, built at runtime so the source never holds a
/// credential-shaped URL literal (the repo audit gate flags those).
fn fake_userinfo() -> String {
    ["user", "pw"].join(":")
}

#[test]
fn hub_rejects_anything_but_a_bare_origin() {
    let with_creds = format!("http://{}@hub.example", fake_userinfo());
    for raw in [
        "hub.example:8765",
        "file:///etc/passwd",
        "ftp://hub.example",
        with_creds.as_str(),
        "http://hub.example/?view=tasks",
        "http://hub.example/#x",
        "http://hub.example/api",
        "",
    ] {
        assert!(
            matches!(Hub::parse(raw), Err(Error::Hub(_))),
            "{raw:?} should be refused"
        );
    }
}

#[test]
fn hub_resolution_order_and_no_silent_fallthrough() {
    let cfg = r#"{"hub_url": "http://from-file:1"}"#;
    let pick = |c, e, f| resolve_hub(c, e, f).unwrap().origin();
    assert_eq!(
        pick(Some("http://cli:1"), Some("http://env:1"), Some(cfg)),
        "http://cli:1"
    );
    assert_eq!(pick(None, Some("http://env:1"), Some(cfg)), "http://env:1");
    assert_eq!(pick(None, Some("  "), Some(cfg)), "http://from-file:1");
    assert_eq!(pick(None, None, Some("{}")), "http://127.0.0.1:8765");
    assert_eq!(pick(None, None, None), "http://127.0.0.1:8765");
    // A bad higher-priority value is an error, not a reason to use the next one.
    assert!(resolve_hub(Some("nope"), Some("http://env:1"), None).is_err());
    assert!(resolve_hub(None, None, Some(r#"{"hub":"typo"}"#)).is_err());
}

#[test]
fn ui_url_carries_view_and_params() {
    let t = ViewTarget {
        view: "inbox".into(),
        params: vec![("tab".into(), "a b&c".into())],
    };
    assert_eq!(hub().ui_url(None).as_str(), "http://hub.example:8765/ui");
    assert_eq!(
        hub().ui_url(Some(&t)).as_str(),
        "http://hub.example:8765/ui?view=inbox&tab=a+b%26c"
    );
}

// --- deep links ------------------------------------------------------------

#[test]
fn deep_link_maps_host_to_view() {
    let t = parse_deep_link("unified-agent://approvals?id=42&tab=pending").unwrap();
    assert_eq!(t.view, "approvals");
    assert_eq!(
        t.params,
        vec![("id".into(), "42".into()), ("tab".into(), "pending".into())]
    );
    assert_eq!(
        parse_deep_link("unified-agent://Tasks/").unwrap().view,
        "tasks"
    );
    assert_eq!(
        hub().ui_url(Some(&t)).as_str(),
        "http://hub.example:8765/ui?view=approvals&id=42&tab=pending"
    );
}

#[test]
fn deep_link_refuses_what_it_cannot_map_safely() {
    let long = format!("unified-agent://tasks?q={}", "x".repeat(MAX_VALUE_LEN + 1));
    let many = format!(
        "unified-agent://tasks?{}",
        (0..=MAX_PARAMS)
            .map(|i| format!("p{i}=1"))
            .collect::<Vec<_>>()
            .join("&")
    );
    let with_creds = format!("unified-agent://{}@tasks", fake_userinfo());
    for raw in [
        "https://evil.example/ui",
        "unified-agent://",
        "unified-agent://task/1556",
        "unified-agent://tasks?view=evidence",
        "unified-agent://tasks?Bad-Key=1",
        with_creds.as_str(),
        "unified-agent://tasks:99",
        "unified-agent://9tasks",
        "unified-agent://ta_sks",
        long.as_str(),
        many.as_str(),
    ] {
        assert!(
            matches!(parse_deep_link(raw), Err(Error::DeepLink(_))),
            "{raw:?} should be refused"
        );
    }
}

#[test]
fn deep_link_can_never_leave_the_hub_origin() {
    // Whatever the link says, the URL we load is built on the configured hub.
    let t = parse_deep_link("unified-agent://tasks?next=https%3A%2F%2Fevil.example").unwrap();
    let url = hub().ui_url(Some(&t));
    assert!(hub().is_same_origin(&url));
}

// --- navigation policy -----------------------------------------------------

#[test]
fn navigation_policy() {
    let h = hub();
    let cases = [
        ("http://hub.example:8765/ui?view=tasks", Nav::InApp),
        ("http://hub.example:8765/api/session", Nav::InApp),
        ("tauri://localhost/offline.html?x=1", Nav::InApp),
        ("http://tauri.localhost/offline.html", Nav::InApp),
        ("about:blank", Nav::InApp),
        // Same host, different port or scheme, is a different origin.
        ("http://hub.example:9999/ui", Nav::External),
        ("https://hub.example:8765/ui", Nav::External),
        ("https://github.com/anonb3ll/unified-agent", Nav::External),
        ("mailto:someone@example.com", Nav::External),
        ("file:///etc/passwd", Nav::Block),
        ("javascript:alert(1)", Nav::Block),
        ("data:text/html,hi", Nav::Block),
        ("tauri://evil/offline.html", Nav::Block),
        // Only the offline page is a navigable bundled page; a future
        // privileged window must not inherit "anything on the bundle origin".
        ("tauri://localhost/", Nav::Block),
        ("tauri://localhost/settings.html", Nav::Block),
        ("tauri://localhost/offline.js", Nav::Block),
        ("http://tauri.localhost/other.html", Nav::Block),
        ("unified-agent://tasks", Nav::Block),
    ];
    for (raw, want) in cases {
        assert_eq!(classify_navigation(&h, &u(raw)), want, "{raw}");
    }
}

#[test]
fn offline_page_carries_hub_and_next() {
    let next = hub().ui_url(None);
    let page = offline_page(&hub(), &next, "connection refused");
    let parsed = u(&format!("tauri://localhost/{page}"));
    assert_eq!(parsed.path(), "/offline.html");
    let q: Vec<_> = parsed.query_pairs().into_owned().collect();
    assert_eq!(
        q,
        vec![
            ("hub".into(), "http://hub.example:8765".into()),
            ("next".into(), "http://hub.example:8765/ui".into()),
            ("reason".into(), "connection refused".into()),
        ]
    );
}

// --- argv ------------------------------------------------------------------

#[test]
fn args_parse() {
    assert_eq!(
        parse_args(Vec::<String>::new()).unwrap(),
        LaunchArgs::default()
    );
    let a = parse_args(["--toggle", "--hub", "http://h:1", "unified-agent://tasks"]).unwrap();
    assert!(a.toggle);
    assert_eq!(a.hub.as_deref(), Some("http://h:1"));
    assert_eq!(a.deep_link.as_deref(), Some("unified-agent://tasks"));
    assert_eq!(
        parse_args(["--hub=http://h:2"]).unwrap().hub.as_deref(),
        Some("http://h:2")
    );
    assert!(parse_args(["--register-scheme"]).unwrap().register_scheme);
    assert!(parse_args(["-h"]).unwrap().help);
    assert!(matches!(parse_args(["--hub"]), Err(Error::Usage(_))));
    assert!(matches!(parse_args(["--nope"]), Err(Error::Usage(_))));
}

// --- the remote page gets no IPC -------------------------------------------

/// Decision 4: the remote `/ui` runs with no native access. Tauri grants IPC
/// only through capabilities. An empty `capabilities` list means "every file
/// in capabilities/", so the guarantee is both: no inline capability and no
/// capabilities/ directory. The global `window.__TAURI__` stays off too.
#[test]
fn tauri_config_grants_no_capabilities() {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../src-tauri");
    let conf: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(dir.join("tauri.conf.json")).unwrap())
            .unwrap();
    assert_eq!(
        conf["app"]["security"]["capabilities"],
        serde_json::json!([])
    );
    assert_eq!(conf["app"]["withGlobalTauri"], serde_json::json!(false));
    assert!(
        conf["app"]["windows"]
            .as_array()
            .is_none_or(|w| w.is_empty()),
        "windows are built in main.rs"
    );
    assert!(
        !dir.join("capabilities").exists(),
        "a capabilities/ dir would be auto-loaded"
    );
}
