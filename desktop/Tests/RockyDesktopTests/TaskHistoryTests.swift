import Foundation
import XCTest
@testable import RockyDesktop

final class TaskHistoryTests: XCTestCase {
    @MainActor
    func testArchivedTasksStayOutOfRecentAndActiveHistory() throws {
        let model = AppModel()
        let completed = try goal("completed", status: "succeeded")
        let running = try goal("running", status: "running")
        let archived = try goal("archived", status: "succeeded", archived: true)
        model.goals = [completed, running, archived]

        XCTAssertEqual(model.visibleGoals.map(\.id), ["completed", "running"])
        model.onlyActive = true
        XCTAssertEqual(model.visibleGoals.map(\.id), ["running"])
        model.showHistory(archived: true)
        XCTAssertEqual(model.visibleGoals.map(\.id), ["archived"])
        model.search = "不存在"
        XCTAssertTrue(model.visibleGoals.isEmpty)
        model.search = "报告"
        XCTAssertEqual(model.visibleGoals.map(\.id), ["archived"])
    }

    @MainActor
    func testOnlyTerminalTasksCanBeManagedWhileConnected() throws {
        let model = AppModel()
        let completed = try goal("completed", status: "succeeded")
        model.goals = [completed]
        XCTAssertFalse(model.canManageHistory(completed))
        model.isConnected = true
        for status in ["succeeded", "failed", "cancelled", "budget_exhausted", "deadline_exceeded"] {
            let item = try goal(status, status: status)
            model.goals = [item]
            XCTAssertTrue(model.canManageHistory(item))
        }
        for status in ["queued", "running", "waiting", "blocked"] {
            let item = try goal(status, status: status)
            model.goals = [item]
            XCTAssertFalse(model.canManageHistory(item))
        }
        model.goals = []
        XCTAssertFalse(model.canManageHistory(completed))
    }

    @MainActor
    func testSwitchingHistoryClearsHiddenSelectionAndNewTaskReturnsToRecent() throws {
        let model = AppModel()
        let completed = try goal("completed", status: "succeeded")
        model.goals = [completed]
        model.selectedID = completed.id
        model.events = [GoalEvent(event_id: 1, state: completed.state)]
        model.showHistory(archived: true)
        XCTAssertNil(model.selectedID)
        XCTAssertTrue(model.events.isEmpty)
        model.newTask()
        XCTAssertFalse(model.showArchived)
    }

    @MainActor
    func testArchiveResponseAfterSelectionChangeKeepsNewSelectionAndClearsPendingState() async throws {
        let (model, session) = connectedModel()
        defer { session.invalidateAndCancel(); HistoryProtocol.handle = nil }
        let completed = try goal("completed", status: "succeeded")
        let other = try goal("other", status: "succeeded")
        model.goals = [completed, other]
        model.selectedID = completed.id
        let started = expectation(description: "Archive dispatched")
        var respond: ((Int, Data) -> Void)?
        HistoryProtocol.handle = { request, reply in
            if request.url?.path.hasSuffix("/archive") == true { respond = reply; started.fulfill() }
            else { reply(200, Data("[]".utf8)) }
        }
        let action = Task { await model.archive(completed, archived: true) }
        await fulfillment(of: [started], timeout: 2)
        XCTAssertFalse(model.canManageHistory(completed))
        await model.delete(completed)
        XCTAssertEqual(model.goals.count, 2)
        model.select(other.id)
        var archived = completed
        archived.archived = true
        try XCTUnwrap(respond)(200, JSONEncoder().encode(archived))
        await action.value
        XCTAssertEqual(model.selectedID, other.id)
        XCTAssertEqual(model.visibleGoals.map(\.id), [other.id])
        XCTAssertTrue(model.historyUpdates.isEmpty)
    }

    @MainActor
    func testDeleteResponseAfterHistorySwitchAndDisconnectKeepsNewSelection() async throws {
        let (model, session) = connectedModel()
        defer { session.invalidateAndCancel(); HistoryProtocol.handle = nil }
        let completed = try goal("completed", status: "succeeded")
        let archived = try goal("archived", status: "succeeded", archived: true)
        model.goals = [completed, archived]
        model.selectedID = completed.id
        let started = expectation(description: "Delete dispatched")
        var respond: ((Int, Data) -> Void)?
        HistoryProtocol.handle = { request, reply in
            if request.httpMethod == "DELETE" { respond = reply; started.fulfill() }
            else { reply(200, Data("[]".utf8)) }
        }
        let action = Task { await model.delete(completed) }
        await fulfillment(of: [started], timeout: 2)
        model.showHistory(archived: true)
        model.select(archived.id)
        model.isConnected = false
        try XCTUnwrap(respond)(200, Data(#"{"deleted":true}"#.utf8))
        await action.value
        XCTAssertEqual(model.selectedID, archived.id)
        XCTAssertEqual(model.visibleGoals.map(\.id), [archived.id])
        XCTAssertEqual(model.goals.map(\.id), [archived.id])
        XCTAssertFalse(model.isConnected)
        XCTAssertTrue(model.historyUpdates.isEmpty)
    }

    @MainActor
    func testRefreshStartedBeforeArchiveCannotPutTaskBackIntoRecent() async throws {
        let (model, session) = connectedModel()
        defer { session.invalidateAndCancel(); HistoryProtocol.handle = nil }
        let completed = try goal("completed", status: "succeeded")
        model.goals = [completed]
        let started = expectation(description: "Refresh waiting for settings")
        var respond: ((Int, Data) -> Void)?
        var archived = completed
        archived.archived = true
        let recentPayload = try JSONEncoder().encode([completed])
        let archivedPayload = try JSONEncoder().encode(archived)
        HistoryProtocol.handle = { request, reply in
            if request.url?.path == "/provider-settings" { respond = reply; started.fulfill() }
            else if request.url?.path.hasSuffix("/archive") == true { reply(200, archivedPayload) }
            else {
                let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
                reply(200, items.first { $0.name == "archived" }?.value == "true" ? Data("[]".utf8) : recentPayload)
            }
        }
        let refresh = Task { await model.refresh() }
        await fulfillment(of: [started], timeout: 2)
        await model.archive(completed, archived: true)
        try XCTUnwrap(respond)(200, Self.settingsJSON)
        await refresh.value
        XCTAssertTrue(model.goals.first?.isArchived == true)
        XCTAssertTrue(model.visibleGoals.isEmpty)
    }

    @MainActor
    func testRefreshWhileArchiveIsPendingCannotReplaceHistory() async throws {
        let (model, session) = connectedModel()
        defer { session.invalidateAndCancel(); HistoryProtocol.handle = nil }
        let completed = try goal("completed", status: "succeeded")
        model.goals = [completed]
        let started = expectation(description: "Archive is pending")
        var respond: ((Int, Data) -> Void)?
        HistoryProtocol.handle = { request, reply in
            if request.url?.path.hasSuffix("/archive") == true { respond = reply; started.fulfill() }
            else if request.url?.path == "/provider-settings" { reply(200, Self.settingsJSON) }
            else { reply(200, Data("[]".utf8)) }
        }
        let action = Task { await model.archive(completed, archived: true) }
        await fulfillment(of: [started], timeout: 2)
        await model.refresh()
        XCTAssertEqual(model.goals.map(\.id), [completed.id])
        var archived = completed
        archived.archived = true
        try XCTUnwrap(respond)(200, JSONEncoder().encode(archived))
        await action.value
        XCTAssertTrue(model.goals.first?.isArchived == true)
    }

    @MainActor
    func testRejectedHistoryMutationRetainsTaskAndAllowsRetry() async throws {
        let (model, session) = connectedModel()
        defer { session.invalidateAndCancel(); HistoryProtocol.handle = nil }
        let completed = try goal("completed", status: "succeeded")
        model.goals = [completed]
        model.selectedID = completed.id
        HistoryProtocol.handle = { _, reply in reply(409, Data("{}".utf8)) }
        await model.archive(completed, archived: true)
        XCTAssertEqual(model.goals, [completed])
        XCTAssertEqual(model.selectedID, completed.id)
        XCTAssertTrue(model.historyUpdates.isEmpty)
        XCTAssertTrue(model.canManageHistory(completed))
        XCTAssertNotNil(model.error)
        await model.delete(completed)
        XCTAssertEqual(model.goals, [completed])
        XCTAssertTrue(model.historyUpdates.isEmpty)
    }

    @MainActor
    func testUnchangedPollKeepsHistoryAndOnlyAppliedListsAreRevalidated() async throws {
        let (model, session) = connectedModel()
        defer { session.invalidateAndCancel(); HistoryProtocol.handle = nil }
        let completed = try goal("completed", status: "succeeded")
        let payload = try JSONEncoder().encode([completed])
        var validators: [String?] = []
        HistoryProtocol.handle = { request, reply in
            guard request.url?.path == "/goals" else { return reply(200, Self.settingsJSON) }
            let archived = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!.first { $0.name == "archived" }?.value == "true"
            let validator = request.value(forHTTPHeaderField: "If-None-Match")
            if !archived { validators.append(validator) }
            if validator == HistoryProtocol.tag(archived) { reply(304, Data()) }
            else { reply(200, archived ? Data("[]".utf8) : payload) }
        }
        await model.refresh()
        XCTAssertEqual(model.goals, [completed])
        await model.refresh()
        XCTAssertEqual(model.goals, [completed])
        XCTAssertTrue(model.isConnected)
        XCTAssertEqual(validators, [nil, HistoryProtocol.tag(false)])
    }

    @MainActor
    func testDiscardedRefreshDoesNotCommitItsTag() async throws {
        let (model, session) = connectedModel()
        defer { session.invalidateAndCancel(); HistoryProtocol.handle = nil }
        let completed = try goal("completed", status: "succeeded")
        model.goals = [completed]
        let started = expectation(description: "Archive is pending")
        var respond: ((Int, Data) -> Void)?
        var validators: [String?] = []
        HistoryProtocol.handle = { request, reply in
            if request.url?.path.hasSuffix("/archive") == true { respond = reply; started.fulfill() }
            else if request.url?.path == "/provider-settings" { reply(200, Self.settingsJSON) }
            else {
                if request.url?.query?.contains("archived=false") == true {
                    validators.append(request.value(forHTTPHeaderField: "If-None-Match"))
                }
                reply(200, Data("[]".utf8))
            }
        }
        let action = Task { await model.archive(completed, archived: true) }
        await fulfillment(of: [started], timeout: 2)
        await model.refresh()
        var archived = completed
        archived.archived = true
        try XCTUnwrap(respond)(200, JSONEncoder().encode(archived))
        await action.value
        await model.refresh()
        // The first response was discarded, so the next poll must fetch the list again.
        XCTAssertEqual(validators, [nil, nil])
        XCTAssertTrue(model.goals.isEmpty)
    }

    @MainActor
    private func connectedModel() -> (AppModel, URLSession) {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [HistoryProtocol.self]
        let session = URLSession(configuration: configuration)
        let client = APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "test", session: session)
        let model = AppModel(client: client)
        model.isConnected = true
        return (model, session)
    }

    private static let settingsJSON = Data(#"{"provider":"ollama","model":"test","has_api_key":false,"credential_storage":"test","providers":[]}"#.utf8)

    private func goal(_ id: String, status: String, archived: Bool = false) throws -> Goal {
        var goal = try JSONDecoder().decode(Goal.self, from: Data(DesktopContractTests.goalJSON.utf8))
        goal.goal_id = id
        goal.state.status = status
        goal.archived = archived
        goal.state.artifacts = []
        return goal
    }
}

private final class HistoryProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var storedHandler: ((URLRequest, @escaping (Int, Data) -> Void) -> Void)?
    static var handle: ((URLRequest, @escaping (Int, Data) -> Void) -> Void)? {
        get { lock.lock(); defer { lock.unlock() }; return storedHandler }
        set { lock.lock(); defer { lock.unlock() }; storedHandler = newValue }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    /// Every goal list is served under a fixed per-filter validator.
    static func tag(_ archived: Bool) -> String { #""list-\#(archived)""# }

    override func startLoading() {
        guard let handler = Self.handle else { return }
        handler(request) { [weak self] status, data in
            guard let self else { return }
            var headers = ["Content-Type": "application/json"]
            if self.request.url?.path == "/goals" {
                headers["ETag"] = Self.tag(self.request.url?.query?.contains("archived=true") == true)
            }
            let response = HTTPURLResponse(url: self.request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: data)
            self.client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func stopLoading() {}
}
