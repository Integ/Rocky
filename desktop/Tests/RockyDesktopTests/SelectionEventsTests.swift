import Combine
import Foundation
import XCTest
@testable import RockyDesktop

final class SelectionEventsTests: XCTestCase {
    private var session: URLSession!

    override func setUp() {
        super.setUp()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [SelectionProtocol.self]
        session = URLSession(configuration: configuration)
    }

    override func tearDown() {
        session.invalidateAndCancel()
        SelectionProtocol.handle = nil
        super.tearDown()
    }

    @MainActor
    func testPollingAppendsDeltaAndEmptyDeltaRetainsHistoryAndPendingCommand() async throws {
        let model = try makeModel()
        let initial = try eventData([2, 7])
        let delta = try eventData([11])
        var cursors: [Int] = []
        SelectionProtocol.handle = { request, reply in
            guard request.url?.path.hasSuffix("/events") == true else { reply(200, Data("[]".utf8)); return }
            let cursor = Self.cursor(request)
            cursors.append(cursor)
            reply(200, cursors.count == 1 ? initial : cursors.count == 3 ? delta : Data("[]".utf8))
        }

        await model.loadSelection()
        model.pendingCommand = "等待接收"
        await model.loadSelection()
        XCTAssertEqual(model.events.map(\.id), [2, 7])
        XCTAssertEqual(model.pendingCommand, "等待接收")

        await model.loadSelection()
        XCTAssertEqual(model.events.map(\.id), [2, 7, 11])
        XCTAssertNil(model.pendingCommand)
        await model.loadSelection()
        XCTAssertEqual(model.events.map(\.id), [2, 7, 11])
        XCTAssertEqual(cursors, [0, 7, 7, 11])
    }

    @MainActor
    func testFailedLoadKeepsCursorAndRetriesUncommittedDelta() async throws {
        let model = try makeModel()
        let initial = try eventData([5])
        let delta = try eventData([8])
        var cursors: [Int] = []
        var messageRequests = 0
        SelectionProtocol.handle = { request, reply in
            if request.url?.path.hasSuffix("/events") == true {
                cursors.append(Self.cursor(request))
                reply(200, cursors.count == 1 ? initial : delta)
            } else {
                messageRequests += 1
                reply(messageRequests == 2 ? 503 : 200, Data("[]".utf8))
            }
        }

        await model.loadSelection()
        model.pendingCommand = "等待接收"
        await model.loadSelection()
        XCTAssertEqual(model.events.map(\.id), [5])
        XCTAssertEqual(model.pendingCommand, "等待接收")
        XCTAssertNotNil(model.error)

        await model.loadSelection()
        XCTAssertEqual(model.events.map(\.id), [5, 8])
        XCTAssertEqual(cursors, [0, 5, 5])
        XCTAssertNil(model.pendingCommand)
    }

    @MainActor
    func testOlderOverlappingLoadCannotOverwriteNewerHistoryOrError() async throws {
        let model = try makeModel()
        let initial = try eventData([3, 8])
        let started = expectation(description: "First event request waits")
        var replyToOlder: ((Int, Data) -> Void)?
        var cursors: [Int] = []
        SelectionProtocol.handle = { request, reply in
            guard request.url?.path.hasSuffix("/events") == true else { reply(200, Data("[]".utf8)); return }
            cursors.append(Self.cursor(request))
            if cursors.count == 1 {
                replyToOlder = reply
                started.fulfill()
            } else {
                reply(200, cursors.count == 2 ? initial : Data("[]".utf8))
            }
        }

        let older = Task { await model.loadSelection() }
        await fulfillment(of: [started], timeout: 2)
        await model.loadSelection()
        try XCTUnwrap(replyToOlder)(503, Data("{}".utf8))
        await older.value

        XCTAssertEqual(model.events.map(\.id), [3, 8])
        XCTAssertNil(model.error)
        await model.loadSelection()
        XCTAssertEqual(cursors, [0, 0, 8])
        XCTAssertEqual(model.events.map(\.id), [3, 8])
    }

    @MainActor
    func testSwitchingSelectionResetsCursorAndIgnoresPreviousSelectionResponse() async throws {
        let model = try makeModel()
        var other = try goal("other")
        other.state.revision = 12
        model.goals.append(other)
        let initial = try eventData([5])
        let otherEvents = try eventData([12])
        let oldSelectionStarted = expectation(description: "Old selection request waits")
        let otherLoaded = expectation(description: "New selection history loaded")
        let firstReloaded = expectation(description: "Returning selection history reloaded")
        var replyToOldSelection: ((Int, Data) -> Void)?
        var requests: [String] = []
        SelectionProtocol.handle = { request, reply in
            guard request.url?.path.hasSuffix("/events") == true else { reply(200, Data("[]".utf8)); return }
            let path = request.url!.path
            requests.append("\(path)?after=\(Self.cursor(request))")
            if requests.count == 1 {
                reply(200, initial)
            } else if requests.count == 2 {
                replyToOldSelection = reply
                oldSelectionStarted.fulfill()
            } else if path.contains("/other/") {
                reply(200, Self.cursor(request) == 0 ? otherEvents : Data("[]".utf8))
            } else {
                reply(200, initial)
            }
        }
        var returning = false
        let subscription = model.$events.sink { events in
            if events.map(\.id) == [12] { otherLoaded.fulfill() }
            if returning, events.map(\.id) == [5] { firstReloaded.fulfill() }
        }
        defer { subscription.cancel() }

        await model.loadSelection()
        let older = Task { await model.loadSelection() }
        await fulfillment(of: [oldSelectionStarted], timeout: 2)
        model.select("other")
        await fulfillment(of: [otherLoaded], timeout: 2)
        try XCTUnwrap(replyToOldSelection)(200, eventData([6]))
        await older.value
        XCTAssertEqual(model.events.map(\.id), [12])
        await model.loadSelection()

        returning = true
        model.select("first")
        await fulfillment(of: [firstReloaded], timeout: 2)
        XCTAssertEqual(model.events.map(\.id), [5])
        XCTAssertEqual(requests, [
            "/goals/first/events?after=0", "/goals/first/events?after=5",
            "/goals/other/events?after=0", "/goals/other/events?after=12",
            "/goals/first/events?after=0",
        ])
    }

    @MainActor
    private func makeModel() throws -> AppModel {
        let client = APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "test", session: session)
        let model = AppModel(client: client)
        model.goals = [try goal("first")]
        model.selectedID = "first"
        return model
    }

    private func goal(_ id: String) throws -> Goal {
        var goal = try JSONDecoder().decode(Goal.self, from: Data(DesktopContractTests.goalJSON.utf8))
        goal.goal_id = id
        goal.state.status = "running"
        goal.state.artifacts = []
        return goal
    }

    private func eventData(_ ids: [Int]) throws -> Data {
        let state = try goal("first").state
        let events = try ids.map { id -> [String: Any] in
            var state = state
            state.revision = id
            return ["event_id": id, "state": try JSONSerialization.jsonObject(with: JSONEncoder().encode(state))]
        }
        return try JSONSerialization.data(withJSONObject: events)
    }

    private static func cursor(_ request: URLRequest) -> Int {
        let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
        return Int(items.first { $0.name == "after" }!.value!)!
    }
}

private final class SelectionProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var storedHandler: ((URLRequest, @escaping (Int, Data) -> Void) -> Void)?
    static var handle: ((URLRequest, @escaping (Int, Data) -> Void) -> Void)? {
        get { lock.lock(); defer { lock.unlock() }; return storedHandler }
        set { lock.lock(); defer { lock.unlock() }; storedHandler = newValue }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let handler = Self.handle else { return }
        handler(request) { [weak self] status, data in
            guard let self else { return }
            let response = HTTPURLResponse(url: self.request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: data)
            self.client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func stopLoading() {}
}
