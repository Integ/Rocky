import Foundation
import XCTest
@testable import RockyDesktop

final class StorageDiagnosticsTests: XCTestCase {
    private let fixture = #"{"scanned_at":"2026-11-02T07:00:00+00:00","total_bytes":16384,"logical_bytes":20000,"categories":[{"key":"deliveries","bytes":16384,"logical_bytes":20000,"files":2}],"complete":false,"limited":true,"unreadable_entries":1,"skipped_links":3,"scan_seconds":3.001,"cached":false}"#

    private func session() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [InterceptingProtocol.self]
        return URLSession(configuration: configuration)
    }

    func testPartialStorageIsLabeledAsALowerBoundAndDeliveryFilesAreSeparate() throws {
        let usage = try JSONDecoder().decode(StorageUsage.self, from: Data(fixture.utf8))
        XCTAssertFalse(usage.complete)
        XCTAssertTrue(usage.coverageLabel.contains("至少"))
        XCTAssertEqual(usage.categories.first?.title, "交付文件快照")
        XCTAssertEqual(usage.categories.first?.files, 2)
        XCTAssertEqual(usage.unreadable_entries, 1)
    }

    func testStorageRefreshAndBinaryExportUseAuthenticatedReadOnlyRequests() async throws {
        let session = session()
        defer { session.invalidateAndCancel(); InterceptingProtocol.handle = nil }
        let api = APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "storage-test", session: session)
        let zip = Data([0x50, 0x4b, 0x03, 0x04, 0, 0xff])
        let fixture = Data(fixture.utf8)
        InterceptingProtocol.handle = { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer storage-test")
            XCTAssertEqual(request.cachePolicy, .reloadIgnoringLocalCacheData)
            let parts = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
            switch parts.path {
            case "/storage":
                XCTAssertEqual(parts.queryItems?.first?.value, "true")
                return (200, fixture)
            case "/diagnostics/export": return (200, zip)
            default: XCTFail("Unexpected endpoint"); return (404, Data())
            }
        }
        let usage = try await api.storageUsage(refresh: true)
        XCTAssertEqual(usage.total_bytes, 16384)
        let downloaded = try await api.downloadDiagnostics()
        XCTAssertEqual(downloaded, zip)
    }

    @MainActor
    func testFailedRefreshKeepsLastMeasurementAndExportCanStillSucceed() async throws {
        let session = session()
        defer { session.invalidateAndCancel(); InterceptingProtocol.handle = nil }
        let api = APIClient(baseURL: URL(string: "http://127.0.0.1:18741")!, token: "storage-test", session: session)
        let state = StorageDiagnosticsModel(api: api)
        let fixture = Data(fixture.utf8)
        var reads = 0
        InterceptingProtocol.handle = { request in
            if request.url?.path == "/diagnostics/export" { return (200, Data("ZIP".utf8)) }
            reads += 1
            return reads == 2 ? (503, Data("private failure".utf8)) : (200, fixture)
        }
        await state.refresh(force: false)
        let original = try XCTUnwrap(state.usage)
        await state.refresh()
        XCTAssertEqual(state.usage, original)
        XCTAssertNotNil(state.error)
        XCTAssertFalse(state.loading)
        XCTAssertFalse(state.error?.contains("private failure") ?? true)
        let data = try await api.downloadDiagnostics()
        XCTAssertEqual(data, Data("ZIP".utf8))
        await state.refresh()
        XCTAssertNil(state.error)
    }
}
