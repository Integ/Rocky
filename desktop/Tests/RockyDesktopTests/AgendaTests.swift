import Foundation
import XCTest
@testable import RockyDesktop

final class AgendaTests: XCTestCase {
    /// Responses as the service sends them (rocky.agenda PlanView and ScheduleView).
    static let planJSON = #"""
    {"plan_id":"242acd6eb7a24674956287b765061db9","objective":"调研门锁\n细节","folders":[],"files":[],"status":"attention","reason":"第 1 步没有完成","priority":0,"replan":true,"steps":[{"step_id":"b95d706910e2","title":"调研","objective":"收集资料","status":"failed","goal_id":"8f0d7b1e-0000-4000-8000-000000000001","attempts":0,"summary":"","outputs":[],"started_at":null,"finished_at":null},{"step_id":"8e633051ee5e","title":"成文","objective":"写报告","status":"pending","goal_id":null,"attempts":0,"summary":"","outputs":[],"started_at":null,"finished_at":null}],"notes":["资料显示需要先做对比"],"revisions":1,"review_pending":false,"planning_tokens":1200,"draft_failures":0,"retry_at":null,"created_at":"2026-10-30T10:10:35+00:00","updated_at":"2026-10-30T10:10:35+00:00","revision":4}
    """#
    static let scheduleJSON = #"""
    {"schedule_id":"2962f9bcee4a4e108540b9612c3b562b","name":"日报","contract":{"objective":"写日报\n\n（这是定时任务“日报”在 2026-11-02 09:00 的一次运行。）","acceptance_criteria":["完整"],"deliverables":[{"path":"report.md","min_bytes":1,"contains":[]}],"allowed_tools":["read_file"],"allowed_domains":[],"folders":[],"files":[],"limits":{"max_steps":100,"max_model_calls":150,"max_tokens":500000,"timeout_seconds":3600},"max_steps":null,"max_model_calls":null,"max_output_tokens":null,"deadline_seconds":null,"max_wait_seconds":3600,"max_no_progress":3},"recurrence":{"kind":"weekly","start_at":null,"every_minutes":null,"time":"09:00","weekdays":[1,3],"timezone":"Asia/Shanghai"},"enabled":true,"anchor_at":"2026-10-30T10:10:35+00:00","next_run_at":"2026-11-02T01:00:00+00:00","last_run_at":null,"last_goal_id":null,"recent_goal_ids":[],"runs":3,"skipped":1,"last_skipped_at":null,"created_at":"2026-10-30T10:10:35+00:00","updated_at":"2026-10-30T10:10:35+00:00","revision":1}
    """#

    func testPlanDecodesWithStepStatesAndOwnerAttention() throws {
        let plan = try JSONDecoder().decode(Plan.self, from: Data(Self.planJSON.utf8))
        XCTAssertEqual(plan.title, "调研门锁")
        XCTAssertTrue(plan.needsOwner)
        XCTAssertEqual(plan.statusLabel, "需要你处理")
        XCTAssertEqual(plan.steps.map(\.statusLabel), ["未完成", "等待"])
        XCTAssertEqual(plan.doneCount, 0)
        XCTAssertEqual(plan.notes?.last, "资料显示需要先做对比")
    }

    func testScheduleDecodesAndDescribesItsRhythm() throws {
        let schedule = try JSONDecoder().decode(Schedule.self, from: Data(Self.scheduleJSON.utf8))
        XCTAssertEqual(schedule.recurrence.summary, "周一、周三 09:00")
        XCTAssertEqual(schedule.nextRun, Date(timeIntervalSince1970: 1_793_581_200))
        XCTAssertEqual(schedule.historyText, "已运行 3 次，1 次因上次未结束而跳过")
        XCTAssertEqual(schedule.contract.withoutScheduleNote.objective, "写日报")
        XCTAssertEqual(Recurrence(kind: "interval", every_minutes: 120).summary, "每 2 小时")
        XCTAssertEqual(Recurrence(kind: "interval", every_minutes: 2880).summary, "每 2 天")
        XCTAssertEqual(Recurrence(kind: "daily", time: "08:30").summary, "每天 08:30")
    }

    func testRequestsEncodeOnlyServiceFields() throws {
        let request = PlanRequest(objective: "调研", steps: [PlanStepDraft(title: "第一步", objective: "收集")])
        let plan = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertEqual(Set(plan.keys), ["objective", "steps", "folders", "files", "replan"])
        let step = try XCTUnwrap((plan["steps"] as? [[String: Any]])?.first)
        XCTAssertEqual(Set(step.keys), ["title", "objective"])

        let once = Recurrence(kind: "once", start_at: Recurrence.stamp(Date(timeIntervalSince1970: 1_793_581_200)), timezone: "Asia/Shanghai")
        let encoded = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(once)) as? [String: Any])
        XCTAssertEqual(encoded["start_at"] as? String, "2026-11-02T01:00:00Z")
        XCTAssertNil(encoded["every_minutes"])
        XCTAssertEqual(encoded["timezone"] as? String, "Asia/Shanghai")
    }

    @MainActor
    func testAgendaRequestsUseTheServiceRoutes() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [AgendaProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); AgendaProtocol.handle = nil }
        var seen: [String] = []
        AgendaProtocol.handle = { request in
            seen.append("\(request.httpMethod ?? "") \(request.url?.path ?? "")")
            switch request.url?.path {
            case "/plans": return (200, Data("[\(Self.planJSON)]".utf8))
            case "/schedules": return (200, Data("[\(Self.scheduleJSON)]".utf8))
            case "/trigger-rules", "/trigger-decisions": return (200, Data("[]".utf8))
            case "/trigger-decisions/page": return (200, Data(#"{"items":[],"total":0,"next_cursor":null}"#.utf8))
            case let path? where path.hasPrefix("/plans/"): return (200, Data(Self.planJSON.utf8))
            default: return (409, Data("{}".utf8))
            }
        }
        let api = APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "t", session: session)
        let agenda = AgendaModel(api: { api })
        await agenda.refresh()
        XCTAssertEqual(agenda.plans.map(\.id), ["242acd6eb7a24674956287b765061db9"])
        XCTAssertEqual(agenda.schedules.map(\.name), ["日报"])
        XCTAssertEqual(agenda.attentionCount, 1)
        let retried = await agenda.perform { _ = try await $0.planAction("242acd6eb7a24674956287b765061db9", "retry") }
        XCTAssertTrue(retried)
        let ran = await agenda.perform { _ = try await $0.runSchedule("2962f9bcee4a4e108540b9612c3b562b") }
        XCTAssertFalse(ran)
        XCTAssertNotNil(agenda.error)
        XCTAssertTrue(seen.contains("POST /plans/242acd6eb7a24674956287b765061db9/retry"))
        XCTAssertTrue(seen.contains("POST /schedules/2962f9bcee4a4e108540b9612c3b562b/run"))
    }
}

private final class AgendaProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var stored: ((URLRequest) throws -> (Int, Data))?
    static var handle: ((URLRequest) throws -> (Int, Data))? {
        get { lock.lock(); defer { lock.unlock() }; return stored }
        set { lock.lock(); defer { lock.unlock() }; stored = newValue }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let handler = try XCTUnwrap(Self.handle)
            let (status, data) = try handler(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}

extension AgendaTests {
    @MainActor
    func testEventInboxCountsAllPendingItemsAndRefreshPreservesPage() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [AgendaProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); AgendaProtocol.handle = nil }
        let contract = try JSONDecoder().decode(Goal.self, from: Data(DesktopContractTests.goalJSON.utf8)).contract
        let rule = TriggerRule(rule_id: "r1", name: "Build", source: "event", match: "build.failed", mode: "observe", contract: contract, revision: 1, problem: "")
        func item(_ id: String) -> TriggerDecision {
            TriggerDecision(decision_id: id, rule_id: "r1", rule_name: "Build", rule_revision: 1, event: TriggerEvent(event_id: id, name: "build.failed", summary: "Failed"), mode: "observe", outcome: "observed", received_at: "now", reason: "")
        }
        let first = item("oldest"), second = item("page-two")
        var cursors: [String?] = []
        AgendaProtocol.handle = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer paging-test")
            switch request.url!.path {
            case "/plans", "/schedules": return (200, Data("[]".utf8))
            case "/trigger-rules": return (200, try JSONEncoder().encode([rule]))
            case "/trigger-decisions/page":
                let values = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
                if values.first(where: { $0.name == "pending_only" })?.value == "true" {
                    let cursor = values.first { $0.name == "cursor" }?.value
                    cursors.append(cursor)
                    XCTAssertEqual(values.first { $0.name == "limit" }?.value, "25")
                    let page = cursor == nil ? TriggerDecisionPage(items: [first], total: 141, next_cursor: "page+two/=cursor") : TriggerDecisionPage(items: [second], total: 141)
                    return (200, try JSONEncoder().encode(page))
                }
                return (200, try JSONEncoder().encode(TriggerDecisionPage(items: [], total: 200)))
            default: return (404, Data())
            }
        }
        let api = APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "paging-test", session: session)
        let model = AgendaModel(api: { api })
        await model.refresh()
        XCTAssertEqual(model.pendingTriggerCount, 141)
        XCTAssertEqual(model.attentionCount, 141)
        XCTAssertEqual(model.pendingTriggerDecisions.map(\.id), ["oldest"])
        await model.navigateTriggers(pending: true, direction: 1)
        XCTAssertEqual(model.pendingTriggerDecisions.map(\.id), ["page-two"])
        await model.refresh()
        XCTAssertEqual(model.pendingFeed.cursor, "page+two/=cursor")
        XCTAssertEqual(model.pendingFeed.previousCursors.count, 1)
        await model.navigateTriggers(pending: true, direction: -1)
        XCTAssertEqual(model.pendingTriggerDecisions.map(\.id), ["oldest"])
        XCTAssertEqual(cursors, [nil, "page+two/=cursor", "page+two/=cursor", nil])
    }

    @MainActor
    func testEventPageFailureKeepsVisibleItemsAndCount() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [AgendaProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); AgendaProtocol.handle = nil }
        var failing = false
        AgendaProtocol.handle = { request in
            if request.url!.path == "/trigger-decisions/page" {
                return failing ? (503, Data()) : (200, Data(#"{"items":[],"total":123,"next_cursor":"next"}"#.utf8))
            }
            return (200, Data("[]".utf8))
        }
        let api = APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "t", session: session)
        let model = AgendaModel(api: { api })
        await model.refresh()
        failing = true
        await model.navigateTriggers(pending: true, direction: 1)
        XCTAssertEqual(model.pendingTriggerCount, 123)
        XCTAssertNil(model.pendingFeed.cursor)
        XCTAssertEqual(model.pendingFeed.previousCursors.count, 0)
        XCTAssertNotNil(model.error)
    }

    func testQuotaLimitedSuggestionRemainsActionableAndStatusDecodes() throws {
        let data = Data(#"{"decision_id":"d1","rule_id":"r1","rule_name":"Build","rule_revision":1,"event":{"event_id":"e1","name":"build.failed","summary":"Failed"},"mode":"auto","outcome":"quota_limited","received_at":"now","reason":"Daily limit reached"}"#.utf8)
        let value = try JSONDecoder().decode(TriggerDecision.self, from: data)
        XCTAssertTrue(value.needsOwner)
        XCTAssertEqual(value.statusLabel, "今日自动启动已达上限")
    }

    func testTriggerRuleDefaultsToObservationAndOldSuggestionsLeaveAttentionCount() throws {
        let goal = try JSONDecoder().decode(Goal.self, from: Data(DesktopContractTests.goalJSON.utf8))
        let request = TriggerRuleRequest(name: "Build", source: "event", match: "build.failed", contract: goal.contract)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertEqual(body["mode"] as? String, "observe")
        XCTAssertEqual(body["debounce_seconds"] as? Int, 30)
        XCTAssertEqual(body["daily_auto_limit"] as? Int, 5)
        XCTAssertEqual(body["daily_timezone"] as? String, "UTC")
        XCTAssertEqual(TriggerRule.label("auto"), "自动执行")
    }

    @MainActor
    func testTriggerRequestsCarryPolicyRevisionAndRefreshNewCollections() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [AgendaProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); AgendaProtocol.handle = nil }
        let goal = try JSONDecoder().decode(Goal.self, from: Data(DesktopContractTests.goalJSON.utf8))
        var rule = TriggerRule(rule_id: "r1", name: "Build", source: "event", match: "build.failed", mode: "observe", contract: goal.contract, revision: 1, problem: "")
        let decision = TriggerDecision(decision_id: "d1", rule_id: "r1", rule_name: "Build", rule_revision: 1,
            event: TriggerEvent(event_id: "e1", name: "build.failed", summary: "Failed"), mode: "observe", outcome: "observed", received_at: "now", reason: "")
        var seen: [String] = []
        AgendaProtocol.handle = { request in
            seen.append(request.url!.path)
            switch request.url!.path {
            case "/plans", "/schedules": return (200, Data("[]".utf8))
            case "/trigger-rules": return (200, try JSONEncoder().encode([rule]))
            case "/trigger-decisions": return (200, try JSONEncoder().encode([decision]))
            case "/trigger-decisions/page":
                let pending = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "pending_only" }?.value == "true"
                let items = pending && (rule.revision != decision.rule_revision || rule.mode == "off") ? [] : [decision]
                return (200, try JSONEncoder().encode(TriggerDecisionPage(items: items, total: items.count)))
            case "/trigger-rules/r1/mode": return (200, try JSONEncoder().encode(rule))
            case "/trigger-decisions/d1/dismiss": return (200, try JSONEncoder().encode(decision))
            default: return (404, Data())
            }
        }
        let api = APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "t", session: session)
        let agenda = AgendaModel(api: { api })
        await agenda.refresh()
        XCTAssertNil(agenda.error)
        XCTAssertEqual(agenda.pendingTriggerDecisions.count, 1)
        XCTAssertEqual(agenda.attentionCount, 1)
        rule.revision = 2
        await agenda.refresh()
        XCTAssertEqual(agenda.pendingTriggerDecisions.count, 0)
        _ = try await api.setTriggerMode("r1", mode: "auto", revision: 2)
        _ = try await api.actOnTrigger("d1", action: "dismiss")
        XCTAssertTrue(seen.contains("/trigger-rules/r1/mode"))
        XCTAssertTrue(seen.contains("/trigger-decisions/d1/dismiss"))
    }
}

extension AgendaTests {
    @MainActor
    func testBrokenActiveFileRulesAppearInAttentionButDisabledRulesDoNot() throws {
        let goal = try JSONDecoder().decode(Goal.self, from: Data(DesktopContractTests.goalJSON.utf8))
        let model = AgendaModel(api: { APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "test") })
        model.triggerRules = [TriggerRule(rule_id: "file", name: "File", source: "file", match: "/tmp/source.csv", mode: "observe", contract: goal.contract, revision: 1, problem: "Missing file")]
        XCTAssertEqual(model.attentionCount, 1)
        XCTAssertEqual(model.problemTriggerRules.count, 1)
        model.triggerRules[0].mode = "off"
        XCTAssertEqual(model.attentionCount, 0)
    }
}
