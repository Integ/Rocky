import Foundation
import XCTest
@testable import RockyDesktop

final class AttentionInboxTests: XCTestCase {
    func testClassificationKeepsApprovalsDistinctAndHidesReviewedOrArchivedResults() throws {
        var goal = try JSONDecoder().decode(Goal.self, from: Data(DesktopContractTests.goalJSON.utf8))
        XCTAssertEqual(GoalAttention(goal: goal, reviewed: false)?.kind, .result)
        XCTAssertNil(GoalAttention(goal: goal, reviewed: true))
        goal.state.status = "failed"
        XCTAssertEqual(GoalAttention(goal: goal, reviewed: false)?.kind, .failed)
        goal.state.status = "budget_exhausted"
        XCTAssertEqual(GoalAttention(goal: goal, reviewed: false)?.kind, .limits)
        goal.state.status = "blocked"
        goal.state.pending_question = PendingQuestion(question_id: "q", text: "Which year?", options: ["2026"], requested_at: "now")
        XCTAssertEqual(GoalAttention(goal: goal, reviewed: false)?.kind, .question)
        goal.state.pending_approval = PendingApproval(fingerprint: "fp", operation_id: "op", action: ProposedAction(tool: "run_command", content: "make"), reason: "approve")
        XCTAssertEqual(GoalAttention(goal: goal, reviewed: false)?.kind, .approval)
        goal.archived = true
        XCTAssertNil(GoalAttention(goal: goal, reviewed: false))
    }

    func testLegacyAndNewMessageStatesDoNotClaimUnreadMessagesWereConsumed() throws {
        func message(_ extra: String) throws -> FeedbackMessage {
            try JSONDecoder().decode(FeedbackMessage.self, from: Data("{\"command_id\":\"c\",\"message\":\"hi\",\"sent\":true\(extra)}".utf8))
        }
        XCTAssertEqual(try message("").statusLabel, "已送达，读取状态未知")
        XCTAssertEqual(try message(",\"delivery_status\":\"delivered\"").statusLabel, "已送达，等待模型读取")
        XCTAssertEqual(try message(",\"delivery_status\":\"consumed\"").statusLabel, "已纳入执行")
    }

    func testChangedQuestionNotifiesOnceWhileStillBlocked() throws {
        var goal = try JSONDecoder().decode(Goal.self, from: Data(DesktopContractTests.goalJSON.utf8))
        goal.state.status = "blocked"
        goal.state.pending_question = PendingQuestion(question_id: "q1", text: "First?", options: [], requested_at: "now")
        var tracker = AttentionTracker()
        _ = tracker.update([goal])
        goal.state.pending_question?.question_id = "q2"
        goal.state.pending_question?.text = "Second?"
        let alerts = tracker.update([goal])
        XCTAssertEqual(alerts.count, 1)
        guard case .blocked(let reason) = alerts.first?.kind else { return XCTFail("expected question") }
        XCTAssertEqual(reason, "Second?")
        XCTAssertTrue(tracker.update([goal]).isEmpty)
    }
}
