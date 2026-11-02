import Foundation
import XCTest
@testable import RockyDesktop

final class DeliveryTests: XCTestCase {
    private var session: URLSession!
    private var api: APIClient { APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "delivery-test", session: session) }

    override func setUp() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [InterceptingProtocol.self]
        session = URLSession(configuration: configuration)
    }
    override func tearDown() {
        session.invalidateAndCancel()
        InterceptingProtocol.handle = nil
        InterceptingProtocol.headers = [:]
    }
    private func goal() throws -> Goal {
        try JSONDecoder().decode(Goal.self, from: Data(DesktopContractTests.goalJSON.utf8))
    }
    private func delivery(_ goal: Goal) -> Delivery {
        Delivery(goal_id: goal.id, revision: goal.state.revision, completed_at: goal.state.updated_at, summary: goal.state.reason, contract: goal.contract, artifacts: goal.state.artifacts, verification_feedback: "Checked", review_status: "pending", feedback: "")
    }

    func testReviewAndHistoryRoutesBindTheConcreteDeliveryVersion() async throws {
        var goal = try goal()
        goal.delivery_review = "accepted"
        var record = delivery(goal)
        record.review_status = "accepted"
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer delivery-test")
            let parts = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
            switch parts.path {
            case "/goals/\(goal.id)/deliveries/89/review":
                XCTAssertEqual(request.httpMethod, "POST")
                return (200, try JSONEncoder().encode(DeliveryReviewReceipt(delivery: record, goal: goal)))
            case "/goals/\(goal.id)/deliveries":
                XCTAssertEqual(parts.queryItems?.first { $0.name == "before_revision" }?.value, "89")
                return (200, try JSONEncoder().encode(DeliveryPage(items: [record], total: 1)))
            case "/goals/\(goal.id)/deliveries/89/artifacts/content":
                XCTAssertEqual(parts.queryItems?.first { $0.name == "path" }?.value, "report.md")
                return (200, Data(#"{"path":"report.md","content":"first edition","truncated":false}"#.utf8))
            case "/goals/\(goal.id)/deliveries/89/artifacts/download": return (200, Data("first edition".utf8))
            default: XCTFail("Unexpected route"); return (404, Data())
            }
        }
        let receipt = try await api.reviewDelivery(goal.id, revision: 89, action: "accept", feedback: "")
        XCTAssertEqual(receipt.goal.delivery_review, "accepted")
        let history = try await api.deliveries(goal.id, before: 89)
        XCTAssertEqual(history.items[0].reviewLabel, "已接受")
        let content = try await api.deliveryArtifact(goal.id, revision: 89, path: "report.md")
        XCTAssertEqual(content.content, "first edition")
        let data = try await api.downloadDeliveryArtifact(goal.id, revision: 89, path: "report.md")
        XCTAssertEqual(String(data: data, encoding: .utf8), "first edition")
    }

    @MainActor
    func testOpeningAResultKeepsItPendingUntilExplicitAcceptance() async throws {
        var goal = try goal()
        goal.state.artifacts = []
        goal.delivery_review = "pending"
        var reviewRequests = 0
        InterceptingProtocol.handle = { request in
            if request.url!.path.hasSuffix("/review") { reviewRequests += 1 }
            return request.url!.path.hasSuffix("/diagnostics") ? (503, Data()) : (200, Data("[]".utf8))
        }
        let model = AppModel(client: api)
        model.goals = [goal]
        await model.select(goal.id)?.value
        XCTAssertEqual(model.attentionItems.count, 1)
        XCTAssertEqual(model.goals[0].delivery_review, "pending")
        XCTAssertEqual(reviewRequests, 0)
        model.goals[0].delivery_review = "accepted"
        XCTAssertTrue(model.attentionItems.isEmpty)
        model.goals[0].state.revision += 1
        model.goals[0].delivery_review = "pending"
        XCTAssertEqual(model.attentionItems.count, 1)
    }

    @MainActor
    func testSameArtifactPathRefreshesWhenTheVerifiedHashChanges() async throws {
        var goal = try goal()
        var reads = 0
        InterceptingProtocol.handle = { request in
            if request.url!.path.hasSuffix("/artifacts/content") {
                reads += 1
                let text = reads == 1 ? "first edition" : "second edition"
                return (200, try JSONSerialization.data(withJSONObject: ["path": "report.md", "content": text, "truncated": false]))
            }
            return request.url!.path.hasSuffix("/diagnostics") ? (503, Data()) : (200, Data("[]".utf8))
        }
        let model = AppModel(client: api)
        model.goals = [goal]
        model.selectedID = goal.id
        await model.loadSelection()
        XCTAssertEqual(model.artifactContents["report.md"], "first edition")
        goal.state.artifacts[0].sha256 = "changed"
        goal.state.revision += 1
        model.goals = [goal]
        await model.loadSelection()
        XCTAssertEqual(model.artifactContents["report.md"], "second edition")
        XCTAssertEqual(reads, 2)
    }
}
