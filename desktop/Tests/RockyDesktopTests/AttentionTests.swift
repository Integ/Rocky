import Foundation
import XCTest
@testable import RockyDesktop

final class AttentionTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_800_000_000)

    func testFirstListSetsTheBaselineWithoutAlerts() throws {
        var tracker = AttentionTracker()
        XCTAssertEqual(tracker.update([try goal("a", status: "blocked"), try goal("b", status: "succeeded")], now: start), [])
    }

    func testNewApprovalRequestAlertsOnceAndAnsweringClearsIt() throws {
        var tracker = AttentionTracker()
        _ = tracker.update([try goal("a", status: "running")], now: start)
        let waiting = try goal("a", status: "blocked", approval: "fp-1", unsandboxed: true)
        let alerts = tracker.update([waiting], now: start + 10)
        XCTAssertEqual(alerts.count, 1)
        XCTAssertEqual(alerts.first?.identifier, "attention:a")
        guard case .approval(let summary, let unsandboxed) = alerts.first?.kind else { return XCTFail("expected approval") }
        XCTAssertTrue(summary.contains("make build"))
        XCTAssertTrue(unsandboxed)
        XCTAssertEqual(tracker.update([waiting], now: start + 20), [])
        // A different request on the same task is a new alert.
        let next = try goal("a", status: "blocked", approval: "fp-2")
        XCTAssertEqual(tracker.update([next], now: start + 30).count, 1)
    }

    func testBlockedQuestionAlertsAndRemindsWhileItKeepsWaiting() throws {
        var tracker = AttentionTracker()
        _ = tracker.update([try goal("a", status: "running")], now: start)
        let blocked = try goal("a", status: "blocked")
        guard case .blocked = tracker.update([blocked], now: start + 60).first?.kind else { return XCTFail("expected blocked") }
        XCTAssertEqual(tracker.update([blocked], now: start + 2 * 3600), [])
        guard case .reminder(let since) = tracker.update([blocked], now: start + 60 + 3 * 3600).first?.kind else {
            return XCTFail("expected reminder")
        }
        XCTAssertEqual(since, start + 60)
        XCTAssertEqual(tracker.update([blocked], now: start + 60 + 4 * 3600), [])
    }

    func testTasksWaitingAtLaunchAreRemindedLater() throws {
        var tracker = AttentionTracker()
        let blocked = try goal("a", status: "blocked")
        _ = tracker.update([blocked], now: start)
        XCTAssertEqual(tracker.update([blocked], now: start + 3600), [])
        guard case .reminder = tracker.update([blocked], now: start + 3 * 3600).first?.kind else { return XCTFail("expected reminder") }
    }

    func testFinishingAlertsOnlyOnTheTransition() throws {
        var tracker = AttentionTracker()
        _ = tracker.update([try goal("a", status: "running")], now: start)
        let done = try goal("a", status: "budget_exhausted")
        let alerts = tracker.update([done], now: start + 5)
        guard case .finished(let status, let label) = alerts.first?.kind else { return XCTFail("expected finished") }
        XCTAssertEqual(status, "budget_exhausted")
        XCTAssertEqual(label, "已达预算上限")
        XCTAssertFalse(alerts[0].playsSound)
        XCTAssertEqual(tracker.update([done], now: start + 10), [])
    }

    func testArchivedTasksAreIgnored() throws {
        var tracker = AttentionTracker()
        _ = tracker.update([], now: start)
        XCTAssertEqual(tracker.update([try goal("a", status: "blocked", archived: true)], now: start + 1), [])
    }

    @MainActor
    func testNotificationTapOpensTheTask() throws {
        let delivery = RecordingDelivery()
        let model = AppModel(delivery: delivery)
        model.goals = [try goal("a", status: "blocked"), try goal("b", status: "running"), try goal("c", status: "blocked", archived: true)]
        XCTAssertEqual(model.attentionGoals.map(\.id), ["a"])
        XCTAssertNotNil(delivery.onOpen)
    }

    private func goal(
        _ id: String, status: String, approval: String? = nil, unsandboxed: Bool = false, archived: Bool = false
    ) throws -> Goal {
        var goal = try JSONDecoder().decode(Goal.self, from: Data(DesktopContractTests.goalJSON.utf8))
        goal.goal_id = id
        goal.state.status = status
        goal.archived = archived
        goal.state.pending_approval = approval.map {
            PendingApproval(
                fingerprint: $0, operation_id: "op",
                action: ProposedAction(tool: "run_command", content: "make build", sandbox: unsandboxed ? false : nil),
                reason: "needs approval"
            )
        }
        return goal
    }
}

@MainActor
private final class RecordingDelivery: AttentionDelivery {
    var onOpen: (@MainActor (String) -> Void)?
    func deliver(_ alerts: [AttentionAlert]) {}
    func updateBadge(_ count: Int) {}
}
