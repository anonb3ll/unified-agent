# Per-project delivery modes (task #1026)

Copy to `~/.unified_agent/project_delivery.yaml` (or point
`UA_PROJECT_DELIVERY_FILE` at another path). Absent file = every project
stays on the historical global posture: `no-mistakes` with `yolo` off.

```yaml
default:
  mode: no-mistakes   # no-mistakes | direct-pr | local-only
  yolo: false         # when true, green non-red-tier local work auto-merges

projects:
  unified-agent:
    mode: direct-pr
    yolo: false
  # browser:
  #   mode: local-only
  #   yolo: true
```

String shorthand is also accepted: `unified-agent: "direct-pr+yolo"`.

A task note can override for one item: `[delivery: mode=local-only yolo=on]`.
