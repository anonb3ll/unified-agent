//! Unified Agent desktop, slice S0: a native window over the hub's `/ui`.
//!
//! The remote page gets no IPC (decision 4): `tauri.conf.json` declares no
//! capabilities and no `capabilities/` directory exists, which
//! `ua-desktop-core`'s tests enforce. Everything native (the tray, toggling,
//! deep links, window state) is driven from Rust, never from the page.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_window_state::{AppHandleExt, StateFlags};
use ua_desktop_core::{self as core, Hub, LaunchArgs, Nav};
use url::Url;

const MAIN: &str = "main";
const PROBE_TIMEOUT: Duration = Duration::from_millis(1500);

fn main() {
    // WebKitGTK's DMA-BUF renderer dies with "Error 71 (Protocol error)
    // dispatching to Wayland display" on NVIDIA under Hyprland (seen on the
    // Linux desktop desktop, GTX 1070). Turn it off unless the user chose otherwise.
    // Set before any thread exists, so mutating the environment is sound.
    #[cfg(target_os = "linux")]
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }

    let args = match core::parse_args(std::env::args().skip(1)) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{e}\n\n{}", core::USAGE);
            std::process::exit(2);
        }
    };
    if args.help {
        println!("{}", core::USAGE);
        return;
    }
    let hub = match load_hub(&args) {
        Ok(h) => h,
        Err(e) => {
            eprintln!("unified-agent-desktop: {e}");
            std::process::exit(2);
        }
    };

    let state_flags = StateFlags::all() & !StateFlags::VISIBLE;
    let hub_for_setup = hub.clone();
    tauri::Builder::default()
        // Must be registered first: a second launch (a Hyprland `--toggle`
        // bind, or a unified-agent:// link on Linux) hands its argv to us
        // and exits.
        .plugin(tauri_plugin_single_instance::init(
            move |app, argv, _cwd| {
                handle_second_launch(app, argv);
            },
        ))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(
            tauri_plugin_window_state::Builder::default()
                .with_state_flags(state_flags)
                .build(),
        )
        .manage(hub)
        .setup(move |app| {
            let handle = app.handle().clone();
            if args.register_scheme {
                register_scheme(&handle);
                handle.exit(0);
                return Ok(());
            }
            let first = args.deep_link.as_deref().and_then(view_target_or_log);
            let window = build_window(&handle, &hub_for_setup, first.as_ref())?;
            build_tray(&handle)?;
            // A cold launch always shows the window, `--toggle` included:
            // toggling an app that was not running means opening it.
            let _ = window.show();
            // macOS delivers links as events rather than argv.
            let h = handle.clone();
            handle.deep_link().on_open_url(move |event| {
                for url in event.urls() {
                    if let Some(t) = view_target_or_log(url.as_str()) {
                        open_target(&h, Some(&t));
                    }
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing hides to the tray; Quit in the tray menu exits.
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to run the Unified Agent desktop shell");
}

fn load_hub(args: &LaunchArgs) -> Result<Hub, core::Error> {
    let env = std::env::var(core::HUB_ENV).ok();
    let path = dirs::config_dir().map(|d| d.join("unified-agent-desktop").join("config.json"));
    let file = path.as_ref().and_then(|p| std::fs::read_to_string(p).ok());
    core::resolve_hub(args.hub.as_deref(), env.as_deref(), file.as_deref())
}

fn view_target_or_log(raw: &str) -> Option<core::ViewTarget> {
    core::parse_deep_link(raw)
        .map_err(|e| eprintln!("unified-agent-desktop: {e}"))
        .ok()
}

/// A TCP connect is enough to tell "hub down or unreachable" from "up". The
/// page itself handles auth and HTTP errors.
fn hub_reachable(hub: &Hub) -> Result<(), String> {
    let addrs = hub
        .socket_addr()
        .to_socket_addrs()
        .map_err(|e| format!("cannot resolve the hub host ({e})"))?;
    let mut last = String::from("the hub host has no address");
    for addr in addrs {
        match TcpStream::connect_timeout(&addr, PROBE_TIMEOUT) {
            Ok(_) => return Ok(()),
            Err(e) => last = e.to_string(),
        }
    }
    Err(last)
}

/// The hub page if it answers, otherwise the bundled offline page, which
/// retries on its own and then moves on to `next`.
fn start_url(hub: &Hub, target: Option<&core::ViewTarget>) -> WebviewUrl {
    let next = hub.ui_url(target);
    match hub_reachable(hub) {
        Ok(()) => WebviewUrl::External(next),
        Err(reason) => WebviewUrl::App(core::offline_page(hub, &next, &reason).into()),
    }
}

fn bundled_url(path_and_query: &str) -> Url {
    #[cfg(windows)]
    let base = "http://tauri.localhost/";
    #[cfg(not(windows))]
    let base = "tauri://localhost/";
    Url::parse(base)
        .and_then(|b| b.join(path_and_query))
        .expect("bundled URL parses")
}

fn build_window(
    app: &AppHandle,
    hub: &Hub,
    target: Option<&core::ViewTarget>,
) -> tauri::Result<WebviewWindow> {
    let nav_hub = hub.clone();
    let nav_app = app.clone();
    WebviewWindowBuilder::new(app, MAIN, start_url(hub, target))
        .title("Unified Agent")
        .inner_size(1280.0, 820.0)
        .min_inner_size(720.0, 480.0)
        .visible(false)
        .on_navigation(move |url| match core::classify_navigation(&nav_hub, url) {
            Nav::InApp => true,
            Nav::External => {
                open_external(&nav_app, url);
                false
            }
            Nav::Block => {
                eprintln!(
                    "unified-agent-desktop: blocked navigation to {}",
                    url.scheme()
                );
                false
            }
        })
        .build()
}

fn open_external(_app: &AppHandle, url: &Url) {
    if let Err(e) = tauri_plugin_opener::open_url(url.as_str(), None::<&str>) {
        eprintln!("unified-agent-desktop: could not open {url} in the browser: {e}");
    }
}

/// Navigate the main window to a view (or to `/ui`) and bring it forward.
fn open_target(app: &AppHandle, target: Option<&core::ViewTarget>) {
    let Some(window) = app.get_webview_window(MAIN) else {
        return;
    };
    let hub = app.state::<Hub>();
    let url = match start_url(&hub, target) {
        WebviewUrl::External(u) => u,
        WebviewUrl::App(p) => bundled_url(&p.to_string_lossy()),
        _ => return,
    };
    if let Err(e) = window.navigate(url) {
        eprintln!("unified-agent-desktop: navigation failed: {e}");
    }
    show(&window);
}

fn handle_second_launch(app: &AppHandle, argv: Vec<String>) {
    // argv[0] is the program. A forwarded --hub is ignored: the running
    // instance keeps the hub it started with.
    let args = match core::parse_args(argv.iter().skip(1)) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("unified-agent-desktop: ignoring second launch: {e}");
            return;
        }
    };
    if let Some(t) = args.deep_link.as_deref().and_then(view_target_or_log) {
        open_target(app, Some(&t));
    } else if let Some(window) = app.get_webview_window(MAIN) {
        if args.toggle {
            toggle(&window);
        } else {
            show(&window);
        }
    }
}

fn show(window: &WebviewWindow) {
    let _ = window.unminimize();
    let _ = window.show();
    let _ = window.set_focus();
}

fn toggle(window: &WebviewWindow) {
    // Visible but unfocused (behind other windows) counts as hidden, so a
    // bind always brings it to you first.
    if window.is_visible().unwrap_or(false) && window.is_focused().unwrap_or(false) {
        let _ = window.hide();
    } else {
        show(window);
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let show_i = MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
    let hide_i = MenuItem::with_id(app, "hide", "Hide", true, None::<&str>)?;
    let quit_i = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_i, &hide_i, &quit_i])?;
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("Unified Agent")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| {
            let window = app.get_webview_window(MAIN);
            match event.id().as_ref() {
                "show" => window.iter().for_each(show),
                "hide" => window.iter().for_each(|w| drop(w.hide())),
                "quit" => {
                    let _ = app.save_window_state(StateFlags::all() & !StateFlags::VISIBLE);
                    app.exit(0);
                }
                _ => {}
            }
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                if let Some(w) = tray.app_handle().get_webview_window(MAIN) {
                    toggle(&w);
                }
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

fn register_scheme(app: &AppHandle) {
    #[cfg(any(target_os = "linux", windows))]
    match app.deep_link().register_all() {
        Ok(()) => println!("Registered {}:// for this binary.", core::SCHEME),
        Err(e) => eprintln!(
            "unified-agent-desktop: could not register {}://: {e}",
            core::SCHEME
        ),
    }
    #[cfg(target_os = "macos")]
    {
        let _ = app;
        println!("On macOS the scheme comes from the app bundle's Info.plist; nothing to do.");
    }
}
