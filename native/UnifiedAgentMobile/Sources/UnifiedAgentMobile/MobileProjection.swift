import Foundation

public enum ProjectionFreshness: String, Decodable, Sendable {
    case live, stale, offline, error
}

public enum HealthState: String, Decodable, Sendable {
    case healthy, degraded, unavailable, unknown
}

public enum AttentionSeverity: String, Decodable, Sendable {
    case critical, high, medium, low, none
}

public struct MobileSource: Decodable, Equatable, Sendable {
    public let sourceHost: String
    public let hubOrigin: String
    public let surface: String

    enum CodingKeys: String, CodingKey {
        case sourceHost = "source_host"
        case hubOrigin = "hub_origin"
        case surface
    }
}

public struct MobileAttentionItem: Decodable, Equatable, Identifiable, Sendable {
    public var id: String { itemRef }
    public let itemRef: String
    public let kind: String
    public let severity: AttentionSeverity
    public let ageSeconds: Int?
    public let safeTitle: String
    public let deepLinkRef: String

    enum CodingKeys: String, CodingKey {
        case itemRef = "item_ref"
        case kind, severity
        case ageSeconds = "age_s"
        case safeTitle = "safe_title"
        case deepLinkRef = "deep_link_ref"
    }
}

public struct MobileAttention: Decodable, Equatable, Sendable {
    public let total: Int
    public let highestSeverity: AttentionSeverity
    public let items: [MobileAttentionItem]

    enum CodingKeys: String, CodingKey {
        case total
        case highestSeverity = "highest_severity"
        case items
    }
}

public struct MobileHealth: Decodable, Equatable, Sendable {
    public let state: HealthState
    public let safeSummary: String

    enum CodingKeys: String, CodingKey {
        case state
        case safeSummary = "safe_summary"
    }
}

public struct MobileFallback: Decodable, Equatable, Sendable {
    public let surface: String
    public let deepLinkRef: String

    enum CodingKeys: String, CodingKey {
        case surface
        case deepLinkRef = "deep_link_ref"
    }
}

public struct MobileProjection: Decodable, Equatable, Sendable {
    public let schemaVersion: String
    public let projectionID: String
    public let generatedAt: Date
    public let expiresAt: Date
    public let freshness: ProjectionFreshness
    public let source: MobileSource
    public let attention: MobileAttention
    public let health: MobileHealth
    public let error: String?
    public let fallback: MobileFallback

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case projectionID = "projection_id"
        case generatedAt = "generated_at"
        case expiresAt = "expires_at"
        case freshness, source, attention, health, error, fallback
    }

    /// Kind → allowlisted safeTitle mapping (mirrors mobile_v1.SAFE_TITLES).
    public static let safeTitles: [String: String] = [
        "review_wait": "Review needed",
        "plan_approval_wait": "Plan approval needed",
        "human_merge_wait": "Human merge needed",
        "blocked_task": "Task blocked",
        "executor_degraded": "Executor degraded",
        "inconsistent_completion": "Review state needs attention",
        "approval": "Human decision needed",
        "anomaly": "System anomaly",
        "stale_worktree": "Stale work detected",
        "stale_claim": "Stale claim detected",
        "identity_drift": "Identity check needed",
        "stale_agent": "Agent connection stale",
        "autonomous_cycle_stopped": "Automation stopped",
    ]

    private static let deepLinkPattern = try! NSRegularExpression(
        pattern: #"^ua://control-center(?:/[a-z0-9_./:-]{1,110})?$"#
    )

    public static func decode(
        _ data: Data,
        now: Date = Date()
    ) throws -> MobileProjection {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let projection = try decoder.decode(Self.self, from: data)
        guard projection.schemaVersion == "mobile.v1" else {
            throw MobileProjectionError.unsupportedSchema
        }
        guard projection.expiresAt > projection.generatedAt else {
            throw MobileProjectionError.invalidExpiry
        }
        // Reject projections restored after expiry (cache must not look live forever).
        if projection.expiresAt <= now {
            throw MobileProjectionError.expired
        }
        guard projection.attention.total == projection.attention.items.count else {
            throw MobileProjectionError.invalidAttentionCount
        }
        guard projection.attention.items.allSatisfy({ item in
            Self.isValidDeepLink(item.deepLinkRef)
                && item.safeTitle == Self.safeTitles[item.kind]
        }), Self.isValidDeepLink(projection.fallback.deepLinkRef) else {
            throw MobileProjectionError.invalidSafeReference
        }
        return projection
    }

    private static func isValidDeepLink(_ ref: String) -> Bool {
        let range = NSRange(ref.startIndex..<ref.endIndex, in: ref)
        return deepLinkPattern.firstMatch(in: ref, options: [], range: range) != nil
    }
}

public enum MobileProjectionError: Error, Equatable {
    case unsupportedSchema
    case invalidExpiry
    case invalidAttentionCount
    case invalidSafeReference
    case expired
}
