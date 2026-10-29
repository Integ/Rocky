import Foundation
import XCTest
@testable import RockyDesktop

final class APIClientTests: XCTestCase {
    private var session: URLSession!

    override func setUp() {
        super.setUp()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [InterceptingProtocol.self]
        session = URLSession(configuration: configuration)
    }

    override func tearDown() {
        session.invalidateAndCancel()
        InterceptingProtocol.handle = nil
        InterceptingProtocol.headers = [:]
        super.tearDown()
    }

    private var api: APIClient {
        APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "desktop-test-token", session: session)
    }

    func testTaskSubmissionUsesLocalAuthenticationAndStableIdempotencyHeader() async throws {
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.url?.host, "127.0.0.1")
            XCTAssertEqual(request.url?.path, "/goals")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer desktop-test-token")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Idempotency-Key"), "retry-same-contract")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
            return (202, Data(DesktopContractTests.goalJSON.utf8))
        }
        let goal = try await api.submit(GoalContract(objective: "整理材料", acceptance_criteria: ["完成整理"], deliverables: [Deliverable(path: "summary.md")]), key: "retry-same-contract")
        XCTAssertEqual(goal.state.status, "succeeded")
    }

    func testContractPreparationUsesModelTimeoutAndPreservesGeneratedRequirements() async throws {
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.url?.path, "/goal-contract")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.timeoutInterval, 330)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer desktop-test-token")
            let goal = try JSONDecoder().decode(Goal.self, from: Data(DesktopContractTests.goalJSON.utf8))
            let warnings = [ContractWarning(code: "concern", message: "目标没有提供数据来源"),
                            ContractWarning(code: "time_limit_low", current: 600, suggested: 2700)]
            return (200, try JSONEncoder().encode(ContractReview(contract: goal.contract, warnings: warnings)))
        }
        let review = try await api.prepareContract(GoalDraft(objective: "整理材料"))
        let contract = review.contract
        XCTAssertEqual(review.warnings.map(\.isConcern), [true, false])
        XCTAssertEqual(review.warnings[0].text, "目标没有提供数据来源")
        XCTAssertTrue(review.warnings[1].text.contains("10 分钟"))
        XCTAssertTrue(review.warnings[1].text.contains("45 分钟"))
        XCTAssertEqual(contract.acceptance_criteria, ["来源可核验"])
        XCTAssertEqual(contract.deliverables.first?.path, "report.md")
        XCTAssertEqual(contract.deliverables.first?.min_bytes, 100)
        XCTAssertEqual(contract.deliverables.first?.contains, ["证据"])
    }

    func testEditedContractIsCheckedWithoutModelAndRulesAreAddedAsCommands() async throws {
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            let payload = try JSONSerialization.jsonObject(with: Self.body(request)) as! [String: Any]
            switch request.url?.path {
            case "/goal-contract/check":
                XCTAssertEqual(payload["objective"] as? String, "整理材料")
                let contract = GoalContract(objective: "整理材料", acceptance_criteria: ["完成整理"], deliverables: [Deliverable(path: "summary.md")])
                return (200, try JSONEncoder().encode(ContractReview(contract: contract, warnings: [])))
            case "/approval-rules":
                XCTAssertEqual(payload["kind"] as? String, "command")
                XCTAssertEqual(payload["command"] as? String, "uv run pytest")
                return (201, Data(#"{"rule_id":"r1","kind":"command","command":"uv run pytest","methods":[],"origin":null,"path_prefix":"/"}"#.utf8))
            default:
                XCTFail("unexpected \(request.url?.path ?? "")")
                return (404, Data())
            }
        }
        let checked = try await api.checkContract(GoalContract(objective: "整理材料", acceptance_criteria: ["完成整理"], deliverables: [Deliverable(path: "summary.md")]))
        XCTAssertTrue(checked.warnings.isEmpty)
        try await api.addCommandRule("uv run pytest")
    }

    func testRecentAndArchivedHistoryUseSeparateServerFilters() async throws {
        for archived in [false, true] {
            InterceptingProtocol.handle = { request in
                let parts = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
                XCTAssertEqual(parts.path, "/goals")
                XCTAssertEqual(parts.queryItems?.first { $0.name == "archived" }?.value, String(archived))
                XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer desktop-test-token")
                return (200, Data("[]".utf8))
            }
            let list = try await api.goalList(archived: archived)
            XCTAssertEqual(list?.goals.isEmpty, true)
        }
    }

    func testGoalListRevalidatesWithItsTagAndTreatsNotModifiedAsUnchanged() async throws {
        InterceptingProtocol.headers = ["ETag": #""tag-1""#]
        InterceptingProtocol.handle = { request in
            XCTAssertNil(request.value(forHTTPHeaderField: "If-None-Match"))
            return (200, Data("[\(DesktopContractTests.goalJSON)]".utf8))
        }
        let fetched = try await api.goalList()
        let first = try XCTUnwrap(fetched)
        XCTAssertEqual(first.tag, #""tag-1""#)
        XCTAssertEqual(first.goals.count, 1)

        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "If-None-Match"), #""tag-1""#)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer desktop-test-token")
            return (304, Data())
        }
        let unchanged = try await api.goalList(ifNoneMatch: first.tag)
        XCTAssertNil(unchanged)
        // Without a validator a 304 is not a successful response.
        InterceptingProtocol.handle = { _ in (304, Data()) }
        do {
            _ = try await api.goalList()
            XCTFail("An unrequested 304 must not be accepted")
        } catch {}
    }

    func testApprovalUsesAuthenticatedConcreteOperationEndpoint() async throws {
        let fingerprint = String(repeating: "a", count: 64)
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/goals/goal-1/approve")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer desktop-test-token")
            var body = request.httpBody
            if body == nil, let stream = request.httpBodyStream {
                stream.open()
                defer { stream.close() }
                var bytes = [UInt8](repeating: 0, count: 1024)
                let count = stream.read(&bytes, maxLength: bytes.count)
                body = Data(bytes.prefix(max(0, count)))
            }
            let payload = try JSONSerialization.jsonObject(with: XCTUnwrap(body)) as! [String: String]
            XCTAssertEqual(payload, ["fingerprint": fingerprint])
            return (202, Data(#"{"status":"queued"}"#.utf8))
        }
        try await api.approve("goal-1", fingerprint: fingerprint)
    }

    func testTaskScopedApprovalSendsExplicitScope() async throws {
        let fingerprint = String(repeating: "b", count: 64)
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.url?.path, "/goals/goal-1/approve")
            var body = request.httpBody
            if body == nil, let stream = request.httpBodyStream {
                stream.open()
                defer { stream.close() }
                var bytes = [UInt8](repeating: 0, count: 1024)
                let count = stream.read(&bytes, maxLength: bytes.count)
                body = Data(bytes.prefix(max(0, count)))
            }
            let payload = try JSONSerialization.jsonObject(with: XCTUnwrap(body)) as! [String: String]
            XCTAssertEqual(payload, ["fingerprint": fingerprint, "scope": "task"])
            return (202, Data(#"{"status":"queued"}"#.utf8))
        }
        try await api.approve("goal-1", fingerprint: fingerprint, forTask: true)
    }

    private static func body(_ request: URLRequest) throws -> Data {
        if let body = request.httpBody { return body }
        let stream = try XCTUnwrap(request.httpBodyStream)
        stream.open()
        defer { stream.close() }
        var bytes = [UInt8](repeating: 0, count: 4096)
        let count = stream.read(&bytes, maxLength: bytes.count)
        return Data(bytes.prefix(max(0, count)))
    }

    func testPermanentApprovalAndDenialSendTheirScopes() async throws {
        let fingerprint = String(repeating: "c", count: 64)
        for scope in [ApprovalScope.always, .deny] {
            InterceptingProtocol.handle = { request in
                XCTAssertEqual(request.url?.path, "/goals/goal-1/approve")
                let payload = try JSONSerialization.jsonObject(with: Self.body(request)) as! [String: String]
                XCTAssertEqual(payload, ["fingerprint": fingerprint, "scope": scope.rawValue])
                return (202, Data(#"{"status":"queued"}"#.utf8))
            }
            try await api.approve("goal-1", fingerprint: fingerprint, scope: scope)
        }
    }

    func testBatchApprovalSendsEachTaskAndCountsAcceptedAnswers() async throws {
        let goal = try JSONDecoder().decode(Goal.self, from: Data(DesktopContractTests.goalJSON.utf8))
        let approval = PendingApproval(fingerprint: String(repeating: "d", count: 64), operation_id: "step-0001", action: ProposedAction(tool: "run_command", content: "make deploy"), reason: "review")
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/approvals/batch")
            let payload = try JSONSerialization.jsonObject(with: Self.body(request)) as! [String: [[String: String]]]
            XCTAssertEqual(payload["items"], [["goal_id": goal.goal_id, "fingerprint": approval.fingerprint, "scope": "deferred"]])
            return (202, Data(#"{"results":[{"status":"queued"}]}"#.utf8))
        }
        let accepted = try await api.approveBatch([ApprovalItem(goal: goal, approval: approval, deferred: true)], scope: .deferred)
        XCTAssertEqual(accepted, 1)
    }

    func testApprovalPolicyDecodesRulesAndSavesSettings() async throws {
        let policy = #"{"settings":{"read_only_commands":true,"timeout_minutes":30},"rules":[{"rule_id":"r1","kind":"http","command":null,"methods":["POST"],"origin":"https://api.example.com","path_prefix":"/v1/","note":"","created_at":"2026-10-29T00:00:00+00:00"}],"allowlist":["git status"]}"#
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.url?.path, "/approval-policy")
            if request.httpMethod == "PUT" {
                let payload = try JSONSerialization.jsonObject(with: Self.body(request)) as! [String: Any]
                XCTAssertEqual(payload["timeout_minutes"] as? Int, 0)
                XCTAssertEqual(payload["read_only_commands"] as? Bool, false)
            }
            return (200, Data(policy.utf8))
        }
        let loaded = try await api.approvalPolicy()
        XCTAssertEqual(loaded.rules.first?.summary, "POST https://api.example.com/v1/")
        XCTAssertEqual(loaded.allowlist, ["git status"])
        _ = try await api.saveApprovalSettings(ApprovalSettings(read_only_commands: false, timeout_minutes: 0))
    }

    func testPendingApprovalDescribesReusableScopeAndExpiry() throws {
        let json = #"{"fingerprint":"e","operation_id":"step-1","action":{"tool":"http_request","url":"https://hooks.example.com/a","content":"{}","sandbox":true},"reason":"review","command_prefix":null,"http_scope":{"method":"POST","origin":"https://hooks.example.com"},"requested_at":"2026-10-29T04:00:00+00:00","expires_at":"2026-10-29T04:30:00.250000+00:00"}"#
        let approval = try JSONDecoder().decode(PendingApproval.self, from: Data(json.utf8))
        XCTAssertEqual(approval.reusableScopeText, "POST https://hooks.example.com")
        XCTAssertNotNil(approval.expiryDate)
    }

    func testArchiveRestoreAndDeleteUseLifecycleEndpoints() async throws {
        for archived in [true, false] {
            InterceptingProtocol.handle = { request in
                XCTAssertEqual(request.httpMethod, "POST")
                XCTAssertEqual(request.url?.path, "/goals/goal-1/\(archived ? "archive" : "restore")")
                var payload = try JSONSerialization.jsonObject(with: Data(DesktopContractTests.goalJSON.utf8)) as! [String: Any]
                payload["archived"] = archived
                return (200, try JSONSerialization.data(withJSONObject: payload))
            }
            let goal = try await api.setArchived("goal-1", archived: archived)
            XCTAssertEqual(goal.isArchived, archived)
        }
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.httpMethod, "DELETE")
            XCTAssertEqual(request.url?.path, "/goals/goal-1")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer desktop-test-token")
            return (200, Data(#"{"goal_id":"goal-1","deleted":true}"#.utf8))
        }
        try await api.deleteGoal("goal-1")
    }

    func testProviderSaveKeepsKeyOutOfRequestURLAndDecodesDistinctPresetIDs() async throws {
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.httpMethod, "PUT")
            XCTAssertEqual(request.url?.path, "/provider-settings")
            XCTAssertNil(request.url?.query)
            XCTAssertFalse(request.url!.absoluteString.contains("synthetic-test-credential"))
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer desktop-test-token")
            return (200, Data(Self.providerJSON.utf8))
        }
        let settings = try await api.saveProvider(ProviderUpdate(provider: "openai-compatible", base_url: "https://openrouter.ai/api/v1", model: "anthropic/claude-sonnet-4.5", reasoning_effort: nil, api_key: "synthetic-test-credential"))
        XCTAssertTrue(settings.isConfigured)
        XCTAssertEqual(settings.label, "OpenRouter")
        XCTAssertEqual(Set(settings.providers.map(\.selectionID)).count, 2)
    }

    func testValidationAndServerFailuresNeverExposeServiceResponseBody() async {
        for status in [401, 403, 422, 500, 503] {
            InterceptingProtocol.handle = { _ in
                (status, Data(#"{"detail":"Authorization: synthetic-test-credential","input":{"api_key":"synthetic-test-credential"}}"#.utf8))
            }
            do {
                _ = try await api.saveProvider(ProviderUpdate(provider: "openai-compatible", base_url: "https://models.example/v1", model: "custom-model", reasoning_effort: nil, api_key: "synthetic-test-credential"))
                XCTFail("Expected rejected provider update")
            } catch {
                XCTAssertFalse(error.localizedDescription.contains("synthetic-test-credential"))
                XCTAssertFalse(error.localizedDescription.contains("Authorization"))
                XCTAssertTrue(error.localizedDescription.contains(where: { $0.unicodeScalars.contains { $0.value >= 0x3400 && $0.value <= 0x9fff } }))
            }
        }
    }

    func testArtifactRequestEncodesPathAsOneQueryValue() async throws {
        let path = "笔记/证据 & 结果#1.md"
        InterceptingProtocol.handle = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
            XCTAssertEqual(components.path, "/goals/goal-1/artifacts/content")
            XCTAssertNil(components.fragment)
            XCTAssertEqual(components.queryItems, [URLQueryItem(name: "path", value: path)])
            return (200, try JSONSerialization.data(withJSONObject: ["path": path, "content": "实际产物", "truncated": false]))
        }
        let artifact = try await api.artifact("goal-1", path: path)
        XCTAssertEqual(artifact.path, path)
        XCTAssertEqual(artifact.content, "实际产物")
    }

    func testArtifactDownloadPreservesBytesBeyondPreviewLimit() async throws {
        let fullFile = Data(repeating: 0x41, count: 150_000) + Data([0x00, 0xff, 0x0a])
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.url?.path, "/goals/goal-1/artifacts/download")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer desktop-test-token")
            return (200, fullFile)
        }
        let downloaded = try await api.downloadArtifact("goal-1", path: "report.md")
        XCTAssertEqual(downloaded, fullFile)
        XCTAssertEqual(downloaded.count, 150_003)
    }

    func testEventsContinueAfterFirstHundredUsingServerCursor() async throws {
        let goal = try JSONSerialization.jsonObject(with: Data(DesktopContractTests.goalJSON.utf8)) as! [String: Any]
        let state = goal["state"]!
        InterceptingProtocol.handle = { request in
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            let cursor = try XCTUnwrap(items.first { $0.name == "after" }?.value)
            let ids: [Int]
            switch cursor {
            case "0": ids = Array(1...100)
            case "100": ids = [154]
            default: XCTFail("Unexpected cursor: \(cursor)"); ids = []
            }
            return (200, try JSONSerialization.data(withJSONObject: ids.map { ["event_id": $0, "state": state] }))
        }
        let events = try await api.events("goal-1")
        XCTAssertEqual(events.count, 101)
        XCTAssertEqual(events.last?.event_id, 154)
    }

    func testEventsStartAtSuppliedCursorAndPaginateOnlyNewHistory() async throws {
        let goal = try JSONSerialization.jsonObject(with: Data(DesktopContractTests.goalJSON.utf8)) as! [String: Any]
        let state = goal["state"]!
        var requestedCursors: [Int] = []
        InterceptingProtocol.handle = { request in
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            let cursor = try XCTUnwrap(Int(try XCTUnwrap(items.first { $0.name == "after" }?.value)))
            requestedCursors.append(cursor)
            let ids: [Int]
            switch cursor {
            case 154: ids = Array(200...299)
            case 299: ids = [410]
            default: XCTFail("Unexpected cursor: \(cursor)"); ids = []
            }
            return (200, try JSONSerialization.data(withJSONObject: ids.map { ["event_id": $0, "state": state] }))
        }

        let events = try await api.events("goal-1", after: 154)

        XCTAssertEqual(requestedCursors, [154, 299])
        XCTAssertEqual(events.map(\.id), Array(200...299) + [410])
    }

    func testEventsWithNoDeltaStopAtSuppliedCursor() async throws {
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.url?.query, "after=154")
            return (200, Data("[]".utf8))
        }

        let events = try await api.events("goal-1", after: 154)

        XCTAssertTrue(events.isEmpty)
    }

    private static let providerJSON = #"""
    {
      "provider":"openai-compatible", "base_url":"https://openrouter.ai/api/v1",
      "model":"anthropic/claude-sonnet-4.5", "reasoning_effort":null,
      "has_api_key":true, "credential_storage":"macos-keychain",
      "providers":[
        {"id":"openai-compatible","preset":"deepseek","label":"DeepSeek","base_url":"https://api.deepseek.com/v1","model":"deepseek-chat","requires_api_key":true},
        {"id":"openai-compatible","preset":"openrouter","label":"OpenRouter","base_url":"https://openrouter.ai/api/v1","model":"anthropic/claude-sonnet-4.5","requires_api_key":true}
      ]
    }
    """#
}

private final class InterceptingProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var storedHandler: ((URLRequest) throws -> (Int, Data))?
    static var handle: ((URLRequest) throws -> (Int, Data))? {
        get { lock.lock(); defer { lock.unlock() }; return storedHandler }
        set { lock.lock(); defer { lock.unlock() }; storedHandler = newValue }
    }
    private static var storedHeaders: [String: String] = [:]
    static var headers: [String: String] {
        get { lock.lock(); defer { lock.unlock() }; return storedHeaders }
        set { lock.lock(); defer { lock.unlock() }; storedHeaders = newValue }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let handler = try XCTUnwrap(Self.handle)
            let (status, data) = try handler(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: Self.headers.merging(["Content-Type": "application/json"]) { first, _ in first })!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}
