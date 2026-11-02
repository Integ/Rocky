import Foundation
import XCTest
@testable import RockyDesktop

final class DiagnosticTests: XCTestCase {
    private let fixture = #"{"goal_id":"task","revision":12,"status":"running","stage":"retry","title":"模型请求等待重试","detail":"服务暂时不可用","since":"2026-11-01T12:00:00.123456+00:00","observed_at":"2026-11-01T12:00:10+00:00","retry_at":"2026-11-01T12:00:17+00:00","attempt":2,"priority":"interactive","queue_wait_seconds":4.2,"model_queue":{"capacity":4,"reserved_interactive":1,"running":3,"waiting":2,"interactive_waiting":1},"budget":{"steps_remaining":null,"model_calls_remaining":12,"tokens_remaining":20000,"wall_seconds_remaining":60,"timer_paused":false}}"#

    func testClockHandlesServerFractionsAndCountsDownWithoutGoingNegative() throws {
        let value = try JSONDecoder().decode(GoalDiagnostic.self, from: Data(fixture.utf8))
        let now = try XCTUnwrap(GoalDiagnostic.date("2026-11-01T04:00:30-08:00"))
        XCTAssertEqual(try XCTUnwrap(value.remainingTime(at: now)), 40, accuracy: 0.01)
        XCTAssertEqual(try XCTUnwrap(value.elapsed(at: now)), 29.876544, accuracy: 0.01)
        XCTAssertEqual(value.remainingTime(at: now.addingTimeInterval(120)), 0)
        XCTAssertNil(value.budget.steps_remaining)
    }

    func testDiagnosticEndpointsDecodeAuthenticatedQueueAndTaskDetails() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [InterceptingProtocol.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); InterceptingProtocol.handle = nil }
        let expected = try JSONDecoder().decode(GoalDiagnostic.self, from: Data(fixture.utf8))
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer diagnostic-test")
            switch request.url?.path {
            case "/model-queue": return (200, try JSONEncoder().encode(expected.model_queue))
            case "/goals/task/diagnostics": return (200, try JSONEncoder().encode(expected))
            default: XCTFail("Unexpected route"); return (404, Data())
            }
        }
        let api = APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "diagnostic-test", session: session)
        let queue = try await api.modelQueue()
        XCTAssertEqual(queue.capacity, 4)
        let diagnostic = try await api.diagnostics("task")
        XCTAssertEqual(diagnostic, expected)
    }
}
