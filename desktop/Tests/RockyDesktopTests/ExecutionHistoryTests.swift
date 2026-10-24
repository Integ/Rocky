import Foundation
import XCTest
@testable import RockyDesktop

final class ExecutionHistoryTests: XCTestCase {
    func testReservationCheckpointsDoNotRepeatOrRelabelThePreviousOperation() {
        let result = observation(step: 1)
        let events = [
            event(1, reservedStep: 1),
            event(2, reservedStep: 1, reason: "读取项目文件", observation: result),
            event(3, reservedStep: 2, reason: "读取项目文件", observation: result),
            event(4, reservedStep: 2, reason: " 读取项目文件\n", observation: result),
        ]
        let entries = ExecutionEntry.build(from: events)

        XCTAssertEqual(entries.count, 2)
        XCTAssertNil(entries[0].firstStep)
        XCTAssertEqual(entries[1].id, 2)
        XCTAssertEqual(entries[1].count, 1)
        XCTAssertEqual(entries[1].firstStep, 1)
        XCTAssertEqual(entries[1].lastStep, 1)
        XCTAssertEqual(entries[1].stepLabel, "第 1 步")
        XCTAssertEqual(entries[1].lastTimestamp, events[1].state.updated_at)
        XCTAssertEqual(events.last?.state.steps, 2)
    }

    func testRealConsecutiveRepeatedOperationsAggregateUsingObservedSteps() {
        let events = [
            event(1, reservedStep: 1),
            event(2, reservedStep: 1, reason: "检查状态", observation: observation(step: 1)),
            event(3, reservedStep: 2, reason: "检查状态", observation: observation(step: 1)),
            event(4, reservedStep: 2, reason: "检查状态", observation: observation(step: 2)),
            event(5, reservedStep: 3, reason: "检查状态", observation: observation(step: 2)),
            event(6, reservedStep: 3, reason: "检查状态", observation: observation(step: 3)),
        ]
        let entries = ExecutionEntry.build(from: events)

        XCTAssertEqual(entries.count, 2)
        XCTAssertEqual(entries[1].id, 2)
        XCTAssertEqual(entries[1].count, 3)
        XCTAssertEqual(entries[1].firstStep, 1)
        XCTAssertEqual(entries[1].lastStep, 3)
        XCTAssertEqual(entries[1].stepLabel, "第 1–3 步")
        XCTAssertEqual(entries[1].firstTimestamp, events[1].state.updated_at)
        XCTAssertEqual(entries[1].lastTimestamp, events[5].state.updated_at)
    }

    func testChangedResultsSuccessAndProgressKeysRemainDistinct() {
        let events = [
            event(1, reservedStep: 1, reason: "处理请求", observation: observation(step: 1)),
            event(2, reservedStep: 2, reason: "处理请求", observation: observation(step: 2, success: false)),
            event(3, reservedStep: 3, reason: "处理请求", observation: observation(step: 3, success: false, content: "需要登录")),
            event(4, reservedStep: 4, reason: "处理请求", observation: observation(step: 4, success: false, content: "需要登录", progressKey: "other-target")),
        ]
        let entries = ExecutionEntry.build(from: events)

        XCTAssertEqual(entries.count, 4)
        XCTAssertTrue(entries.allSatisfy { $0.count == 1 })
        XCTAssertEqual(entries.map(\.firstStep), [1, 2, 3, 4])
        XCTAssertEqual(entries[2].observation?.content, "需要登录")
        XCTAssertEqual(entries[3].observation?.progress_key, "other-target")
    }

    func testSameTextSeparatedByAnotherOperationIsNotGloballyDeduplicated() {
        let entries = ExecutionEntry.build(from: [
            event(1, reservedStep: 1, reason: "检查状态", observation: observation(step: 1, content: "A")),
            event(2, reservedStep: 2, reason: "检查状态", observation: observation(step: 2, content: "B")),
            event(3, reservedStep: 3, reason: "检查状态", observation: observation(step: 3, content: "A")),
        ])

        XCTAssertEqual(entries.count, 3)
        XCTAssertEqual(entries.map { $0.observation?.content }, ["A", "B", "A"])
    }

    func testStatusChangesAndTerminalStateRetainRowsWithoutRepeatingCarriedResult() {
        let result = observation(step: 1)
        let entries = ExecutionEntry.build(from: [
            event(1, reservedStep: 1, reason: "等待回复", observation: result),
            event(2, reservedStep: 1, status: "blocked", reason: "等待回复", observation: result),
            event(3, reservedStep: 2, status: "running", reason: "等待回复", observation: result),
            event(4, reservedStep: 2, status: "succeeded", reason: "等待回复", observation: result),
        ])

        XCTAssertEqual(entries.map(\.status), ["running", "blocked", "running", "succeeded"])
        XCTAssertNotNil(entries[0].observation)
        XCTAssertTrue(entries.dropFirst().allSatisfy { $0.observation == nil && $0.firstStep == nil })
        XCTAssertEqual(entries[2].title, "恢复执行")
        XCTAssertEqual(entries[3].title, "等待回复")
        XCTAssertEqual(entries[3].statusLabel, "已完成")
    }

    func testNewReasonWithCarriedObservationIsAStateRow() {
        let result = observation(step: 1)
        let entries = ExecutionEntry.build(from: [
            event(1, reservedStep: 1, reason: "读取文件", observation: result),
            event(2, reservedStep: 2, reason: "开始核验", observation: result),
            event(3, reservedStep: 2, reason: "开始核验", observation: result),
        ])

        XCTAssertEqual(entries.count, 2)
        XCTAssertEqual(entries[1].title, "开始核验")
        XCTAssertEqual(entries[1].stepLabel, "状态")
        XCTAssertNil(entries[1].observation)
    }

    func testArtifactChangesAreRetainedButOrderOnlyChangesAreIgnored() {
        let first = Artifact(path: "report.md", size: 100, sha256: "first")
        let changed = Artifact(path: "report.md", size: 100, sha256: "changed")
        let notes = Artifact(path: "notes.txt", size: 10, sha256: "notes")
        let entries = ExecutionEntry.build(from: [
            event(1, reservedStep: 1, reason: "保存产物", artifacts: [first, notes]),
            event(2, reservedStep: 2, reason: "保存产物", artifacts: [notes, first]),
            event(3, reservedStep: 2, reason: "保存产物", artifacts: [notes, changed]),
        ])

        XCTAssertEqual(entries.count, 2)
        XCTAssertTrue(entries.allSatisfy(\.artifactsChanged))
        XCTAssertEqual(entries[1].artifacts.first(where: { $0.path == "report.md" })?.sha256, "changed")
    }

    func testWaitTimerChangesAndResumeAreRetained() {
        let entries = ExecutionEntry.build(from: [
            event(1, reservedStep: 1, status: "waiting", reason: "等待更新", wakeAt: "2026-10-22T01:00:00Z"),
            event(2, reservedStep: 1, status: "waiting", reason: "等待更新", wakeAt: "2026-10-22T02:00:00Z"),
            event(3, reservedStep: 2, status: "running", reason: "等待更新"),
        ])

        XCTAssertEqual(entries.count, 3)
        XCTAssertEqual(entries[1].wakeAt, "2026-10-22T02:00:00Z")
        XCTAssertEqual(entries[2].title, "恢复执行")
        XCTAssertTrue(entries.allSatisfy { $0.firstStep == nil })
    }

    func testLegacySnapshotsWithoutObservationsCollapseCounterChangesAndStillDecode() throws {
        var payload = try JSONSerialization.jsonObject(with: Data(DesktopContractTests.goalJSON.utf8)) as! [String: Any]
        var state = payload["state"] as! [String: Any]
        state.removeValue(forKey: "observations")
        state.removeValue(forKey: "wake_at")
        payload["state"] = state
        let legacyGoal = try JSONDecoder().decode(Goal.self, from: JSONSerialization.data(withJSONObject: payload))
        XCTAssertNil(legacyGoal.state.observations)
        XCTAssertNil(legacyGoal.state.wake_at)

        let entries = ExecutionEntry.build(from: [
            event(1, reservedStep: 1, reason: "准备执行"),
            event(2, reservedStep: 2, reason: "准备执行"),
            event(3, reservedStep: 3, status: "succeeded", reason: "完成"),
        ])
        XCTAssertEqual(entries.count, 2)
        XCTAssertEqual(entries.map(\.title), ["准备执行", "完成"])
        XCTAssertTrue(entries.allSatisfy { $0.firstStep == nil })
    }

    func testObservationPayloadWithoutProgressKeyRemainsBackwardCompatible() throws {
        let result = try JSONDecoder().decode(ToolObservation.self, from: Data(#"{"step":1,"tool":"read_file","success":true,"content":"文件内容"}"#.utf8))
        XCTAssertNil(result.progress_key)
        var explicitEmptyKey = result
        explicitEmptyKey.progress_key = ""
        let entries = ExecutionEntry.build(from: [
            event(1, reservedStep: 1, reason: "读取文件", observation: result),
            event(2, reservedStep: 2, reason: "读取文件", observation: explicitEmptyKey),
        ])
        XCTAssertEqual(entries.count, 1)
        XCTAssertEqual(entries[0].count, 1)
        XCTAssertEqual(entries[0].lastStep, 1)
    }

    func testAggregationDoesNotMutateRawAuditEventsOrTheirRevisions() {
        let events = [
            event(1, reservedStep: 1, reason: "读取文件", observation: observation(step: 1)),
            event(2, reservedStep: 2, reason: "读取文件", observation: observation(step: 1)),
            event(3, reservedStep: 2, reason: "读取文件", observation: observation(step: 2)),
        ]
        let states = events.map(\.state)
        let entries = ExecutionEntry.build(from: events)

        XCTAssertEqual(entries.count, 1)
        XCTAssertEqual(events.map(\.state), states)
        XCTAssertEqual(events.map(\.id), [1, 2, 3])
        XCTAssertEqual(events.last?.state.revision, 3)
        XCTAssertTrue(ExecutionEntry.build(from: []).isEmpty)
    }

    func testParallelReadBatchShowsEveryObservationFromItsStep() {
        let earlier = observation(step: 1, content: "旧结果")
        let batch = ["a.py", "b.py", "c.py"].map {
            ToolObservation(step: 2, tool: "read_file", success: true, content: $0, progress_key: $0)
        }
        var batchEvent = event(2, reservedStep: 2, reason: "并行读取文件")
        batchEvent.state.observations = [earlier] + batch
        let entries = ExecutionEntry.build(from: [event(1, reservedStep: 1, reason: "读取", observation: earlier), batchEvent])

        XCTAssertEqual(entries.map { $0.observation?.content }, ["旧结果", "a.py", "b.py", "c.py"])
        XCTAssertEqual(Set(entries.map(\.id)).count, entries.count)
        XCTAssertEqual(entries[1].id, 2)
        XCTAssertEqual(entries.dropFirst().map(\.firstStep), [2, 2, 2])
    }

    func testCompactEventProjectionPreservesBatchAndStateHistory() throws {
        let old = observation(step: 1, content: "旧结果")
        let batch = ["a.py", "b.py"].map {
            ToolObservation(step: 2, tool: "read_file", success: true, content: $0, progress_key: $0)
        }
        let artifact = Artifact(path: "report.md", size: 20, sha256: "saved")
        var batchEvent = event(2, reservedStep: 2, reason: "并行读取文件")
        batchEvent.state.observations = [old] + batch
        var reservation = event(3, reservedStep: 3, reason: "准备核验")
        reservation.state.observations = [old] + batch
        var completed = event(5, reservedStep: 3, status: "succeeded", reason: "核验完成", artifacts: [artifact])
        completed.state.observations = [old] + batch
        let original = [
            event(1, reservedStep: 1, reason: "读取文件", observation: old), batchEvent, reservation,
            event(4, reservedStep: 3, status: "waiting", reason: "等待更新", wakeAt: "2026-10-22T02:00:00Z"),
            completed,
        ]
        let fields: Set<String> = [
            "status", "reason", "steps", "model_calls", "input_tokens", "output_tokens",
            "artifacts", "updated_at", "revision", "observations", "wake_at",
        ]
        let payload = try original.map { event -> [String: Any] in
            var state = try JSONSerialization.jsonObject(with: JSONEncoder().encode(event.state)) as! [String: Any]
            state = state.filter { fields.contains($0.key) }
            let latestStep = event.state.observations?.last?.step
            state["observations"] = try event.state.observations?.filter { $0.step == latestStep }.map {
                try JSONSerialization.jsonObject(with: JSONEncoder().encode($0))
            }
            return ["event_id": event.id, "state": state]
        }
        let compact = try JSONDecoder().decode([GoalEvent].self, from: JSONSerialization.data(withJSONObject: payload))
        let expected = ExecutionEntry.build(from: original)
        let actual = ExecutionEntry.build(from: compact)

        XCTAssertEqual(actual.map(\.id), expected.map(\.id))
        XCTAssertEqual(actual.map(\.title), expected.map(\.title))
        XCTAssertEqual(actual.map(\.status), expected.map(\.status))
        XCTAssertEqual(actual.map(\.observation), expected.map(\.observation))
        XCTAssertEqual(actual.map(\.artifacts), expected.map(\.artifacts))
        XCTAssertEqual(actual.map(\.artifactsChanged), expected.map(\.artifactsChanged))
        XCTAssertEqual(actual.map(\.wakeAt), expected.map(\.wakeAt))
        XCTAssertEqual(actual.map(\.stepLabel), expected.map(\.stepLabel))
        XCTAssertEqual(actual.map(\.lastTimestamp), expected.map(\.lastTimestamp))
    }

    private func observation(step: Int, success: Bool = true, content: String = "结果未变化", progressKey: String? = nil) -> ToolObservation {
        ToolObservation(step: step, tool: "http_request", success: success, content: content, progress_key: progressKey)
    }

    private func event(_ id: Int, reservedStep: Int, status: String = "running", reason: String = "", observation: ToolObservation? = nil, artifacts: [Artifact] = [], wakeAt: String? = nil) -> GoalEvent {
        GoalEvent(event_id: id, state: RunState(
            status: status, reason: reason, steps: reservedStep, model_calls: id,
            input_tokens: id * 100, output_tokens: id * 20, artifacts: artifacts,
            updated_at: "2026-10-22T00:00:\(String(format: "%02d", id))Z", revision: id,
            observations: observation.map { [$0] }, wake_at: wakeAt
        ))
    }
}
