import Foundation
import XCTest
@testable import UnifiedAgentMobile

final class MobileProjectionTests: XCTestCase {
    private static let fixtureNow = ISO8601DateFormatter().date(
        from: "2026-08-19T20:01:00Z"
    )!

    func testDecodesBoundedLiveProjection() throws {
        let projection = try MobileProjection.decode(fixture(), now: Self.fixtureNow)
        XCTAssertEqual(projection.attention.total, 1)
        XCTAssertEqual(projection.attention.highestSeverity, .high)
        XCTAssertEqual(projection.health.state, .healthy)
        XCTAssertEqual(projection.attention.items.first?.safeTitle, "Human decision needed")
    }

    func testDecodesEmptyStaleOfflineAndAuthStates() throws {
        let cases = [("stale", "null"), ("offline", #""offline""#),
                     ("error", #""auth_required""#)]
        for (freshness, error) in cases {
            let projection = try MobileProjection.decode(
                fixture(freshness: freshness, error: error, empty: true),
                now: Self.fixtureNow
            )
            XCTAssertEqual(projection.attention.total, 0)
            XCTAssertEqual(projection.attention.highestSeverity, .none)
        }
    }

    func testRejectsUnsupportedSchemaAndUnsafeDeepLink() {
        XCTAssertThrowsError(try MobileProjection.decode(fixture(schema: "mobile.v2")))
        XCTAssertThrowsError(
            try MobileProjection.decode(fixture(deepLink: "https://example.com/private"))
        )
    }

    private func fixture(
        schema: String = "mobile.v1",
        freshness: String = "live",
        error: String = "null",
        empty: Bool = false,
        deepLink: String = "ua://control-center/attention/approval/123"
    ) -> Data {
        let items = empty ? "[]" : """
        [{
          "item_ref":"attention:approval:123",
          "kind":"approval",
          "severity":"high",
          "age_s":42,
          "safe_title":"Human decision needed",
          "deep_link_ref":"\(deepLink)"
        }]
        """
        let severity = empty ? "none" : "high"
        let total = empty ? 0 : 1
        return Data("""
        {
          "schema_version":"\(schema)",
          "projection_id":"mp_test",
          "generated_at":"2026-08-19T20:00:00Z",
          "expires_at":"2026-08-19T20:05:00Z",
          "freshness":"\(freshness)",
          "source":{"source_host":"laptop","hub_origin":"local","surface":"unified-agent"},
          "attention":{"total":\(total),"highest_severity":"\(severity)","items":\(items)},
          "health":{"state":"healthy","safe_summary":"Service available"},
          "error":\(error),
          "fallback":{"surface":"iphone","deep_link_ref":"ua://control-center"}
        }
        """.utf8)
    }
}
