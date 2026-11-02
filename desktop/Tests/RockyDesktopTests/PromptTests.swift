import Foundation
import XCTest
@testable import RockyDesktop

final class PromptTests: XCTestCase {
    private var session: URLSession!
    private var api: APIClient {
        APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "prompt-token", session: session)
    }
    private var snapshot: PromptSnapshot {
        PromptSnapshot(snapshot_id: "snapshot-1", purpose: "planner", modules: [PromptModule(module_id: "planner", title: "执行与交接", version: 1, sha256: "12345678abcdef", loading: "始终加载", text: "fixed application instructions")], instructions: "fixed application instructions", output_schema: #"{"type":"object"}"#)
    }

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

    func testPreviewIsAuthenticatedAndSendsOnlyPurposeAndFixedGuidance() async throws {
        let expected = snapshot
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.url?.path, "/prompts/preview")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer prompt-token")
            var data = request.httpBody ?? Data()
            if data.isEmpty, let stream = request.httpBodyStream {
                stream.open()
                defer { stream.close() }
                var bytes = [UInt8](repeating: 0, count: 2048)
                while stream.hasBytesAvailable {
                    let size = stream.read(&bytes, maxLength: bytes.count)
                    guard size > 0 else { break }
                    data.append(contentsOf: bytes.prefix(size))
                }
            }
            let body = try JSONSerialization.jsonObject(with: data) as! [String: Any]
            XCTAssertEqual(Set(body.keys), ["purpose", "guidance"])
            XCTAssertEqual(body["guidance"] as? [String], ["coding"])
            return (200, try JSONEncoder().encode(expected))
        }
        let actual = try await api.previewPrompt("planner", guidance: ["coding"])
        XCTAssertEqual(actual, expected)
        XCTAssertEqual(actual.modules[0].versionLabel, "v1 · 12345678")
    }

    func testHistoricalMetadataDecodesWithoutModuleTextAndKeepsUpgradeDiff() async throws {
        let saved = snapshot
        var installed = saved
        installed.snapshot_id = "snapshot-2"
        installed.instructions += "\nnew instruction"
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.httpMethod, "GET")
            switch request.url?.path {
            case "/goals/task/prompts":
                return (200, Data(#"[{"snapshot_id":"snapshot-1","purpose":"planner","modules":[{"module_id":"planner","title":"执行与交接","version":1,"sha256":"12345678abcdef","loading":"始终加载"}],"first_used_at":"2026-11-01","last_used_at":"2026-11-02"}]"#.utf8))
            case "/prompt-snapshots/snapshot-1":
                return (200, try JSONEncoder().encode(PromptComparison(snapshot: saved, current: installed, changed: true, diff: "+new instruction")))
            case "/prompt-modules": return (200, try JSONEncoder().encode(saved.modules))
            default: XCTFail("unexpected route"); return (404, Data())
            }
        }
        let uses = try await api.promptUses("task")
        XCTAssertNil(uses[0].modules[0].text)
        let comparison = try await api.promptSnapshot(uses[0].id)
        XCTAssertEqual(comparison.snapshot.instructions, saved.instructions)
        XCTAssertTrue(comparison.changed)
        XCTAssertEqual(comparison.diff, "+new instruction")
        let modules = try await api.promptModules()
        XCTAssertEqual(modules, saved.modules)
    }
}
