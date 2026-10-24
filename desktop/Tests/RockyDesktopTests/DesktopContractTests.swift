import Foundation
import XCTest
@testable import RockyDesktop

final class DesktopContractTests: XCTestCase {
    func testSubmissionOnlyRequiresObjective() throws {
        let draft = try GoalDraft(objective: "  编写报告  ")
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(draft)) as! [String: Any]
        XCTAssertEqual(encoded["objective"] as? String, "编写报告")
        XCTAssertEqual(Set(encoded.keys), ["objective"])
    }

    func testExplicitUnlimitedLimitsRemainReadableForExistingContracts() throws {
        let limits = TaskLimits(max_steps: nil, max_model_calls: nil, max_tokens: nil, timeout_seconds: nil)
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(limits)) as! [String: Any]
        for key in ["max_steps", "max_model_calls", "max_tokens", "timeout_seconds"] {
            XCTAssertTrue(encoded[key] is NSNull)
        }
    }

    func testInvalidObjectivesAreRejectedBeforeModelCall() {
        for objective in ["   ", String(repeating: "字", count: 8001)] {
            XCTAssertThrowsError(try GoalDraft(objective: objective))
        }
    }

    func testDirectContractDefaultsIncludeAllTools() {
        let contract = GoalContract(objective: "整理材料", acceptance_criteria: ["完成"], deliverables: [Deliverable(path: "report.md")])
        XCTAssertEqual(contract.allowed_tools, Self.fullTools)
        XCTAssertTrue(contract.allowed_domains.isEmpty)
    }

    func testNativeModelsDecodeBackendTaskAndEventIncludingUnknownFields() throws {
        let goal = try JSONDecoder().decode(Goal.self, from: Data(Self.goalJSON.utf8))
        XCTAssertEqual(goal.id, "8ed51687-1d78-4c58-8ca9-dccf265b0923")
        XCTAssertEqual(goal.contract.deliverables.first?.path, "report.md")
        XCTAssertEqual(goal.contract.allowed_tools, ["read_file", "write_file", "list_files"])
        XCTAssertEqual(goal.model_settings?.model, "anthropic/claude-sonnet-4.5")
        XCTAssertEqual(goal.model_settings?.provider, "openai-compatible")
        XCTAssertEqual(goal.state.artifacts.first?.sha256, "abc123")
        XCTAssertTrue(goal.state.isTerminal)

        let wrapper = try JSONSerialization.jsonObject(with: Data(Self.goalJSON.utf8)) as! [String: Any]
        let eventsData = try JSONSerialization.data(withJSONObject: [["event_id": 154, "state": wrapper["state"]!]])
        let events = try JSONDecoder().decode([GoalEvent].self, from: eventsData)
        XCTAssertEqual(events.first?.event_id, 154)
        XCTAssertEqual(events.first?.state.revision, 89)
        XCTAssertEqual(events.first?.state.statusLabel, "已完成")
    }

    func testLegacyTaskDoesNotInheritCurrentModelAndBlockedIsResumable() throws {
        var payload = try JSONSerialization.jsonObject(with: Data(Self.goalJSON.utf8)) as! [String: Any]
        payload.removeValue(forKey: "model_settings")
        var state = payload["state"] as! [String: Any]
        state["status"] = "blocked"
        payload["state"] = state
        let legacy = try JSONDecoder().decode(Goal.self, from: JSONSerialization.data(withJSONObject: payload))
        XCTAssertNil(legacy.model_settings)
        XCTAssertFalse(legacy.state.isTerminal)
        XCTAssertEqual(legacy.state.statusLabel, "需要你的回复")
        XCTAssertFalse(legacy.isArchived)
    }

    func testUnlimitedTaskDecodesNullBudgetsAndArchiveMetadata() throws {
        var payload = try JSONSerialization.jsonObject(with: Data(Self.goalJSON.utf8)) as! [String: Any]
        var contract = payload["contract"] as! [String: Any]
        for key in ["max_steps", "max_model_calls", "max_output_tokens", "deadline_seconds"] { contract[key] = NSNull() }
        payload["contract"] = contract
        payload["archived"] = true
        let archived = try JSONDecoder().decode(Goal.self, from: JSONSerialization.data(withJSONObject: payload))
        XCTAssertTrue(archived.isArchived)
        XCTAssertEqual(archived.contract.objective, "整理一份有来源的报告")
        XCTAssertEqual(archived.state.steps, 12)
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(archived.contract)) as! [String: Any]
        XCTAssertNil(encoded["max_steps"])
        XCTAssertNil(encoded["deadline_seconds"])
    }

    private static let fullTools = ["read_file", "write_file", "list_files", "fetch_url", "http_request", "run_command", "append_file", "edit_file"]

    static let goalJSON = #"""
    {
      "goal_id": "8ed51687-1d78-4c58-8ca9-dccf265b0923",
      "contract": {
        "objective": "整理一份有来源的报告",
        "acceptance_criteria": ["来源可核验"],
        "deliverables": [{"path": "report.md", "min_bytes": 100, "contains": ["证据"]}],
        "allowed_tools": ["read_file", "write_file", "list_files"],
        "allowed_domains": [], "max_steps": 20, "max_model_calls": 30,
        "max_output_tokens": 4096, "deadline_seconds": 86400,
        "max_wait_seconds": 3600, "max_no_progress": 3
      },
      "state": {
        "goal_id": "8ed51687-1d78-4c58-8ca9-dccf265b0923", "revision": 89,
        "status": "succeeded", "reason": "报告通过核验", "steps": 12,
        "model_calls": 13, "input_tokens": 1200, "output_tokens": 400,
        "observations": [], "artifacts": [{"path": "report.md", "size": 128, "sha256": "abc123"}],
        "wake_at": null, "updated_at": "2026-10-21T16:00:00+00:00"
      },
      "created_at": "2026-10-21T15:00:00+00:00",
      "model_settings": {
        "model": "anthropic/claude-sonnet-4.5", "reasoning_effort": null,
        "provider": "openai-compatible", "base_url": "https://openrouter.ai/api/v1",
        "credential_id": "opaque-credential-reference"
      }
    }
    """#
}
