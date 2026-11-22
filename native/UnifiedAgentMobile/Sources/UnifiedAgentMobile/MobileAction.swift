import Foundation

/// The bounded verb the wrist may emit. v1 = one safe action.
/// `redirect` / `dispatch` are a later slice and route through the approval
/// queue, not this auto-apply path.
public enum MobileActionVerb: String, Codable, Sendable, CaseIterable {
    case acknowledge
}

/// What the Watch hands the iPhone relay: an allowlisted verb plus references
/// copied verbatim from a `MobileProjection` it has already validated. No
/// credentials, no arguments, no free text, no claim of authorization.
public struct MobileActionIntent: Equatable, Sendable {
    public let verb: MobileActionVerb
    public let itemRef: String
    public let projectionID: String

    public init(verb: MobileActionVerb, itemRef: String, projectionID: String) {
        self.verb = verb
        self.itemRef = itemRef
        self.projectionID = projectionID
    }

    /// Build an acknowledge intent from an attention row the Watch is showing.
    /// Returns nil if the row/projection references are not the opaque grammar
    /// the hub will accept — the Watch fails closed rather than sending junk.
    public static func acknowledge(
        item: MobileAttentionItem,
        projection: MobileProjection
    ) -> MobileActionIntent? {
        guard Self.isItemRef(item.itemRef),
              Self.isProjectionID(projection.projectionID)
        else { return nil }
        return MobileActionIntent(
            verb: .acknowledge,
            itemRef: item.itemRef,
            projectionID: projection.projectionID
        )
    }

    static let itemRefPattern = try! NSRegularExpression(
        pattern: #"^attention:[a-z0-9_]+:[A-Za-z0-9._-]{1,96}$"#
    )
    static let projectionIDPattern = try! NSRegularExpression(
        pattern: #"^mp_[A-Za-z0-9._-]{1,61}$"#
    )
    static let actionIDPattern = try! NSRegularExpression(
        pattern: #"^ma_[A-Za-z0-9._-]{1,61}$"#
    )

    static func isItemRef(_ s: String) -> Bool { matches(Self.itemRefPattern, s) }
    static func isProjectionID(_ s: String) -> Bool { matches(Self.projectionIDPattern, s) }

    private static func matches(_ re: NSRegularExpression, _ s: String) -> Bool {
        re.firstMatch(in: s, range: NSRange(s.startIndex..<s.endIndex, in: s)) != nil
    }
}

/// The `mobile-action.v1` request the iPhone relay POSTs to the hub after a
/// fresh authenticated session. Mirrors `mobile_action_v1.MobileActionRequest`.
public struct MobileActionRequest: Encodable, Equatable, Sendable {
    public let schemaVersion = "mobile-action.v1"
    public let actionID: String
    public let action: MobileActionVerb
    public let itemRef: String
    public let projectionID: String
    public let issuedAt: Date

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case actionID = "action_id"
        case action
        case itemRef = "item_ref"
        case projectionID = "projection_id"
        case issuedAt = "issued_at"
    }

    /// Turn a Watch intent into a hub request, minting the replay nonce.
    public init(intent: MobileActionIntent, now: Date = Date()) {
        self.actionID = Self.newActionID(now: now)
        self.action = intent.verb
        self.itemRef = intent.itemRef
        self.projectionID = intent.projectionID
        self.issuedAt = now
    }

    static func newActionID(now: Date) -> String {
        "ma_\(Int(now.timeIntervalSince1970))_\(UUID().uuidString.prefix(8).lowercased())"
    }

    public func encoded() throws -> Data {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .custom { date, enc in
            var c = enc.singleValueContainer()
            let f = ISO8601DateFormatter()
            f.formatOptions = [.withInternetDateTime]
            try c.encode(f.string(from: date))
        }
        return try encoder.encode(self)
    }
}

/// The `mobile-action-result.v1` envelope the hub returns.
/// Mirrors `mobile_action_v1.MobileActionResult`.
public struct MobileActionResult: Decodable, Equatable, Sendable {
    public enum Status: String, Decodable, Sendable {
        case applied, noop, rejected
    }

    public let schemaVersion: String
    public let actionID: String
    public let status: Status
    public let reason: String?
    public let auditRef: String?
    public let acknowledgedUntil: Date?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case actionID = "action_id"
        case status, reason
        case auditRef = "audit_ref"
        case acknowledgedUntil = "acknowledged_until"
    }

    public static func decode(_ data: Data) throws -> MobileActionResult {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let result = try decoder.decode(Self.self, from: data)
        guard result.schemaVersion == "mobile-action-result.v1" else {
            throw MobileActionError.unsupportedSchema
        }
        if result.status == .rejected, result.auditRef != nil {
            throw MobileActionError.malformedResult
        }
        return result
    }

    /// Whether the Watch should optimistically drop the item from its cached
    /// view (applied or noop both mean "it's handled/gone for now").
    public var isSuppressing: Bool { status == .applied || status == .noop }
}

public enum MobileActionError: Error, Equatable {
    case unsupportedSchema
    case malformedResult
    /// The iPhone relay was not reachable — the Watch keeps its last projection
    /// and shows a transient "open iPhone" affordance. No offline queue.
    case relayUnreachable
    /// The hub rejected the action (see `MobileActionResult.reason`).
    case rejected(String)
}

/// The transport seam. The Watch calls `submit(_:)`; a concrete relay (the
/// iPhone companion, over `WCSession.sendMessage` + an authenticated hub POST)
/// implements it. The Watch target ships only a stub that returns
/// `.relayUnreachable` until the companion app exists — the read-only slice is
/// never blocked on this.
public protocol MobileActionRelay: Sendable {
    /// Submit one intent. Throws `MobileActionError.relayUnreachable` when the
    /// counterpart is not reachable; `.rejected` when the hub refused.
    func submit(_ intent: MobileActionIntent) async throws -> MobileActionResult
}

/// Default Watch-side relay: no companion yet, so every submit is a no-op that
/// keeps the read-only fallback intact.
public struct UnreachableActionRelay: MobileActionRelay {
    public init() {}
    public func submit(_ intent: MobileActionIntent) async throws -> MobileActionResult {
        throw MobileActionError.relayUnreachable
    }
}
