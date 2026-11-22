import Foundation
import XCTest
@testable import UnifiedAgentMobile

final class MobileActionTests: XCTestCase {
    private static let fixtureNow = ISO8601DateFormatter().date(
        from: "2026-09-11T00:40:00Z"
    )!

    // MARK: intent

    func testBuildsAcknowledgeIntentFromAValidatedProjection() throws {
        let projection = try MobileProjection.decode(projectionFixture(), now: Self.fixtureNow)
        let item = try XCTUnwrap(projection.attention.items.first)
        let intent = try XCTUnwrap(
            MobileActionIntent.acknowledge(item: item, projection: projection)
        )
        XCTAssertEqual(intent.verb, .acknowledge)
        XCTAssertEqual(intent.itemRef, item.itemRef)
        XCTAssertEqual(intent.projectionID, projection.projectionID)
    }

    func testIntentFailsClosedOnAJunkReference() {
        let bad = MobileAttentionItem(
            itemRef: "not-an-item-ref", kind: "approval", severity: .high,
            ageSeconds: 1, safeTitle: "Human decision needed",
            deepLinkRef: "ua://control-center"
        )
        let projection = try! MobileProjection.decode(projectionFixture(), now: Self.fixtureNow)
        XCTAssertNil(MobileActionIntent.acknowledge(item: bad, projection: projection))
    }

    // MARK: request envelope

    func testRequestEncodesTheContractShape() throws {
        let intent = MobileActionIntent(
            verb: .acknowledge,
            itemRef: "attention:approval:123",
            projectionID: "mp_test"
        )
        let request = MobileActionRequest(intent: intent, now: Self.fixtureNow)
        let json = try JSONSerialization.jsonObject(
            with: request.encoded()
        ) as! [String: Any]
        XCTAssertEqual(json["schema_version"] as? String, "mobile-action.v1")
        XCTAssertEqual(json["action"] as? String, "acknowledge")
        XCTAssertEqual(json["item_ref"] as? String, "attention:approval:123")
        XCTAssertEqual(json["projection_id"] as? String, "mp_test")
        let actionID = try XCTUnwrap(json["action_id"] as? String)
        XCTAssertTrue(
            MobileActionIntent.actionIDPattern.firstMatch(
                in: actionID,
                range: NSRange(actionID.startIndex..<actionID.endIndex, in: actionID)
            ) != nil
        )
        XCTAssertEqual(Set(json.keys), [
            "schema_version", "action_id", "action", "item_ref", "projection_id", "issued_at",
        ])
    }

    // MARK: result envelope

    func testDecodesAppliedResult() throws {
        let result = try MobileActionResult.decode(Data("""
        {
          "schema_version": "mobile-action-result.v1",
          "action_id": "ma_1_a",
          "status": "applied",
          "reason": null,
          "audit_ref": "01J000",
          "acknowledged_until": "2026-09-11T04:40:00Z"
        }
        """.utf8))
        XCTAssertEqual(result.status, .applied)
        XCTAssertTrue(result.isSuppressing)
        XCTAssertEqual(result.auditRef, "01J000")
    }

    func testDecodesRejectedResultAndRejectsAnAuditRefOnIt() throws {
        let noop = try MobileActionResult.decode(Data("""
        {"schema_version":"mobile-action-result.v1","action_id":"ma_1_a",
         "status":"noop","reason":null,"audit_ref":null,"acknowledged_until":null}
        """.utf8))
        XCTAssertEqual(noop.status, .noop)
        XCTAssertTrue(noop.isSuppressing)

        XCTAssertThrowsError(try MobileActionResult.decode(Data("""
        {"schema_version":"mobile-action-result.v1","action_id":"ma_1_a",
         "status":"rejected","reason":"policy_denied","audit_ref":"01J000",
         "acknowledged_until":null}
        """.utf8)))
    }

    func testRejectsUnsupportedResultSchema() {
        XCTAssertThrowsError(try MobileActionResult.decode(Data("""
        {"schema_version":"mobile-action-result.v2","action_id":"ma_1_a",
         "status":"applied","reason":null,"audit_ref":null,"acknowledged_until":null}
        """.utf8)))
    }

    // MARK: read-only fallback stays intact

    func testDefaultRelayIsUnreachableSoTheReadOnlyPathIsUnchanged() async {
        let relay = UnreachableActionRelay()
        let intent = MobileActionIntent(
            verb: .acknowledge, itemRef: "attention:approval:123", projectionID: "mp_x"
        )
        do {
            _ = try await relay.submit(intent)
            XCTFail("expected relayUnreachable")
        } catch {
            XCTAssertEqual(error as? MobileActionError, .relayUnreachable)
        }
    }

    // MARK: fixtures

    private func projectionFixture() -> Data {
        Data("""
        {
          "schema_version":"mobile.v1",
          "projection_id":"mp_1757548790_0a1b2c3d",
          "generated_at":"2026-09-11T00:39:00Z",
          "expires_at":"2026-09-11T00:44:00Z",
          "freshness":"live",
          "source":{"source_host":"laptop","hub_origin":"local","surface":"unified-agent"},
          "attention":{"total":1,"highest_severity":"high","items":[{
            "item_ref":"attention:approval:123",
            "kind":"approval",
            "severity":"high",
            "age_s":42,
            "safe_title":"Human decision needed",
            "deep_link_ref":"ua://control-center/attention/approval"
          }]},
          "health":{"state":"healthy","safe_summary":"Service available"},
          "error":null,
          "fallback":{"surface":"iphone","deep_link_ref":"ua://control-center"}
        }
        """.utf8)
    }
}
