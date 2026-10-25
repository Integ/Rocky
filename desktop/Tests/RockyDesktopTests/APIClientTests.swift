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
            return (200, try JSONEncoder().encode(goal.contract))
        }
        let contract = try await api.prepareContract(GoalDraft(objective: "整理材料"))
        XCTAssertEqual(contract.acceptance_criteria, ["来源可核验"])
        XCTAssertEqual(contract.deliverables.first?.path, "report.md")
        XCTAssertEqual(contract.deliverables.first?.min_bytes, 100)
        XCTAssertEqual(contract.deliverables.first?.contains, ["证据"])
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
