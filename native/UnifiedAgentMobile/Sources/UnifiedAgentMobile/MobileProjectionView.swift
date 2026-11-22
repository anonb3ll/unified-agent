import SwiftUI

public struct MobileProjectionView: View {
    private let projection: MobileProjection
    private let relay: MobileActionRelay

    /// Item refs the viewer has acknowledged this session — dropped optimistically
    /// from the list until the next projection arrives already filtered.
    @State private var acknowledgedLocally: Set<String> = []
    @State private var pending: MobileAttentionItem?
    @State private var lastActionError: String?

    public init(
        projection: MobileProjection,
        relay: MobileActionRelay = UnreachableActionRelay()
    ) {
        self.projection = projection
        self.relay = relay
    }

    private var visibleItems: [MobileAttentionItem] {
        projection.attention.items.filter { !acknowledgedLocally.contains($0.itemRef) }
    }

    public var body: some View {
        List {
            Section {
                valueRow("Attention", String(visibleItems.count))
                valueRow("Severity", projection.attention.highestSeverity.rawValue)
                valueRow("Health", projection.health.safeSummary)
            }

            if visibleItems.isEmpty {
                Label("No attention", systemImage: "checkmark.circle")
            } else {
                Section("Needs attention") {
                    ForEach(visibleItems) { item in
                        Label(item.safeTitle, systemImage: severitySymbol(item.severity))
                            .swipeActions(edge: .trailing) {
                                // Acknowledge is only offered on a live projection —
                                // a stale/offline projection can't authorize anything.
                                if projection.freshness == .live {
                                    Button {
                                        pending = item
                                    } label: {
                                        Label("Acknowledge", systemImage: "bell.slash")
                                    }
                                }
                            }
                    }
                }
            }

            if let lastActionError {
                Section {
                    Label(lastActionError, systemImage: "iphone.and.arrow.forward")
                } footer: {
                    Text("Open the authenticated iPhone surface to acknowledge.")
                }
            }

            if projection.freshness != .live || projection.error != nil {
                Section {
                    Label(statusText, systemImage: "iphone.and.arrow.forward")
                } footer: {
                    Text("Open the authenticated iPhone or web surface to reconnect.")
                }
            }
        }
        .navigationTitle("Unified Agent")
        .confirmationDialog(
            "Acknowledge this item?",
            isPresented: Binding(
                get: { pending != nil },
                set: { if !$0 { pending = nil } }
            ),
            titleVisibility: .visible,
            presenting: pending
        ) { item in
            Button("Acknowledge") { Task { await acknowledge(item) } }
            Button("Cancel", role: .cancel) { pending = nil }
        } message: { _ in
            Text("It stays open on the desktop — this only hides it from your wrist for a while.")
        }
    }

    private func acknowledge(_ item: MobileAttentionItem) async {
        pending = nil
        lastActionError = nil
        guard let intent = MobileActionIntent.acknowledge(item: item, projection: projection) else {
            lastActionError = "Couldn't acknowledge — invalid reference."
            return
        }
        do {
            let result = try await relay.submit(intent)
            if result.isSuppressing {
                acknowledgedLocally.insert(item.itemRef)
            } else {
                lastActionError = "Couldn't acknowledge — \(result.reason ?? "rejected")."
            }
        } catch MobileActionError.relayUnreachable {
            lastActionError = "Couldn't acknowledge — open iPhone."
        } catch MobileActionError.rejected(let reason) {
            lastActionError = "Couldn't acknowledge — \(reason)."
        } catch {
            lastActionError = "Couldn't acknowledge — try again from iPhone."
        }
    }

    private func valueRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label)
            Spacer()
            Text(value).foregroundStyle(.secondary)
        }
    }

    private var statusText: String {
        switch projection.freshness {
        case .live: "Current"
        case .stale: "Last update is stale"
        case .offline: "Offline"
        case .error: "Reconnect required"
        }
    }

    private func severitySymbol(_ severity: AttentionSeverity) -> String {
        switch severity {
        case .critical, .high: "exclamationmark.triangle.fill"
        case .medium: "exclamationmark.circle"
        case .low: "info.circle"
        case .none: "checkmark.circle"
        }
    }
}
