import Foundation
import XCTest
@testable import RockyDesktop

final class MarkdownReportTests: XCTestCase {
    func testReportKeepsBlockStructureAndInlineFormatting() throws {
        let blocks = MarkdownBlock.parse("# 研究报告\n\n结论：**已完成**，参见 [来源](https://example.com)。\n\n> 需要核实\n\n```swift\nlet count = 2\n```\n\n---")
        XCTAssertEqual(blocks.count, 5)
        XCTAssertEqual(blocks[0].kind, .header(level: 1))
        XCTAssertEqual(String(blocks[0].text.characters), "研究报告")
        XCTAssertEqual(String(blocks[1].text.characters), "结论：已完成，参见 来源。")
        XCTAssertTrue(blocks[1].text.runs.contains { $0.inlinePresentationIntent?.contains(.stronglyEmphasized) == true })
        XCTAssertEqual(blocks[1].text.runs.compactMap(\.link), [URL(string: "https://example.com")!])
        XCTAssertEqual(blocks[2].kind, .blockQuote)
        XCTAssertEqual(String(blocks[2].children[0].text.characters), "需要核实")
        XCTAssertEqual(blocks[3].kind, .codeBlock(languageHint: "swift"))
        XCTAssertEqual(String(blocks[3].text.characters), "let count = 2\n")
        XCTAssertEqual(blocks[4].kind, .thematicBreak)
    }

    func testNestedListsKeepParentsAndNumbering() {
        let blocks = MarkdownBlock.parse("3. 第一项\n   - 子项 **重点**\n4. 第二项")
        XCTAssertEqual(blocks.count, 1)
        XCTAssertEqual(blocks[0].kind, .orderedList)
        let items = blocks[0].children
        XCTAssertEqual(items.map(\.kind), [.listItem(ordinal: 3), .listItem(ordinal: 4)])
        XCTAssertEqual(items[0].children[1].kind, .unorderedList)
        XCTAssertEqual(String(items[0].children[1].children[0].children[0].text.characters), "子项 重点")
    }

    func testTableRetainsRowsCellsAndColumnAlignment() throws {
        let blocks = MarkdownBlock.parse("| 项目 | 结果 |\n| :--- | ---: |\n| **检查** | 通过 |")
        let table = try XCTUnwrap(blocks.first)
        guard case .table(let columns) = table.kind else { return XCTFail("Expected table") }
        XCTAssertEqual(columns.map(\.alignment), [.left, .right])
        XCTAssertEqual(table.children.map(\.kind), [.tableHeaderRow, .tableRow(rowIndex: 1)])
        XCTAssertEqual(table.children.map { $0.children.map { String($0.text.characters) } }, [["项目", "结果"], ["检查", "通过"]])
    }
}
