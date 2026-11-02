import AppKit
import Combine
import SwiftUI

struct StorageCategory: Decodable, Equatable, Identifiable {
    var key: String
    var bytes: Int64
    var logical_bytes: Int64
    var files: Int
    var id: String { key }
    var title: String {
        switch key {
        case "workspaces": "任务工作区"
        case "deliveries": "交付文件快照"
        case "task_data": "任务运行数据"
        case "databases": "数据库与执行历史"
        case "backups": "备份"
        case "logs": "运行日志"
        default: "其他"
        }
    }
    var detail: String {
        switch key {
        case "workspaces": "包含任务文件、依赖和缓存"
        case "deliveries": "各轮核验后保留的交付原件"
        case "task_data": "操作回执、浏览器会话等"
        case "databases": "包含正在使用的数据库辅助文件"
        case "backups": "包含备份过程中使用的临时文件"
        case "logs": "包含轮转后保留的日志"
        default: ""
        }
    }
}

struct StorageUsage: Decodable, Equatable {
    var scanned_at: String
    var total_bytes: Int64
    var logical_bytes: Int64
    var categories: [StorageCategory]
    var complete: Bool
    var limited: Bool
    var unreadable_entries: Int
    var skipped_links: Int
    var scan_seconds: Double
    var cached: Bool
    var coverageLabel: String { complete ? "已统计文件的磁盘占用（估算）" : "部分文件的磁盘占用（至少）" }
}

@MainActor
final class StorageDiagnosticsModel: ObservableObject {
    let api: APIClient
    @Published private(set) var usage: StorageUsage?
    @Published private(set) var loading = false
    @Published private(set) var error: String?

    init(api: APIClient) { self.api = api }

    func refresh(force: Bool = true) async {
        guard !loading else { return }
        loading = true
        defer { loading = false }
        do { usage = try await api.storageUsage(refresh: force); error = nil }
        catch { self.error = error.localizedDescription }
    }
}

struct StorageDiagnosticsView: View {
    @StateObject private var state: StorageDiagnosticsModel
    let dataURL: URL
    @Environment(\.dismiss) private var dismiss
    @State private var exporting = false
    @State private var exportMessage: String?
    @State private var exportFailed = false

    init(api: APIClient, dataURL: URL) {
        _state = StateObject(wrappedValue: StorageDiagnosticsModel(api: api))
        self.dataURL = dataURL
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text("存储与诊断").font(.title2.weight(.semibold))
                    Text("查看本机数据占用，导出排查问题所需的运行统计。").font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
            }.padding(24)
            Divider()
            Form {
                Section {
                    if let usage = state.usage {
                        HStack {
                            Text(usage.coverageLabel)
                            Spacer()
                            Text(ServiceHealthSection.size(usage.total_bytes)).font(.title3.weight(.semibold)).foregroundStyle(Color.rockyAccent)
                        }
                        ForEach(usage.categories) { category in
                            HStack {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(category.title)
                                    Text(category.detail).font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                VStack(alignment: .trailing, spacing: 4) {
                                    Text(ServiceHealthSection.size(category.bytes)).monospacedDigit()
                                    Text("\(category.files) 个文件").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                        Text("统计时间：" + ServiceHealthSection.date(usage.scanned_at) + (usage.cached ? " · 使用最近统计" : ""))
                            .font(.caption).foregroundStyle(.secondary)
                        if usage.limited {
                            Text("文件较多，本次已达到统计上限；显示的用量为下限。").font(.caption).foregroundStyle(.orange)
                        }
                        if usage.unreadable_entries > 0 {
                            Text("\(usage.unreadable_entries) 个项目暂时无法读取或已变化，未纳入完整统计。").font(.caption).foregroundStyle(.orange)
                        }
                    } else if state.loading { ProgressView("正在统计空间…").controlSize(.small) }
                    if let error = state.error {
                        Text((state.usage == nil ? "无法读取空间统计：" : "刷新失败，保留上次统计：") + error).font(.caption).foregroundStyle(.red)
                    }
                    HStack {
                        Button(state.loading ? "正在统计…" : "刷新统计") { Task { await state.refresh() } }.disabled(state.loading)
                        Spacer()
                        Button("打开数据文件夹") { NSWorkspace.shared.open(dataURL) }
                    }
                } header: { Text("空间占用") } footer: {
                    Text("统计任务数据、数据库、日志和备份，包含已存档任务；不含应用、运行环境、外部授权文件夹和符号链接目标。磁盘共享空间可能使估算与 Finder 不同。统计按需进行，最多缓存 60 秒。")
                }
                Section {
                    Button(exporting ? "正在生成诊断包…" : "导出脱敏诊断包…") { Task { await export() } }
                        .disabled(exporting)
                    Text("包含版本、系统类型、服务状态、任务数量、模型排队和空间统计。").font(.caption).foregroundStyle(.secondary)
                    Text("不含任务内容、交付文件、文件名与路径、原始日志、模型配置或密钥。").font(.caption).foregroundStyle(.secondary)
                    if let exportMessage {
                        Text(exportMessage).font(.caption).foregroundStyle(exportFailed ? Color.red : Color.secondary)
                    }
                } header: { Text("诊断包") } footer: {
                    Text("只保存到你选择的位置，不会自动上传。诊断包不是备份，不能用于恢复任务。")
                }
            }.formStyle(.grouped)
            Divider()
            HStack {
                Spacer()
                Button("完成") { dismiss() }.keyboardShortcut(.cancelAction)
            }.padding(16)
        }.frame(width: 660, height: 720).tint(.rockyAccent)
            .task { await state.refresh(force: false) }
    }

    private func export() async {
        let panel = NSSavePanel()
        let stamp = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "-")
        panel.nameFieldStringValue = "rocky-diagnostics-\(stamp).zip"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        exporting = true
        exportMessage = nil
        defer { exporting = false }
        do {
            let data = try await state.api.downloadDiagnostics()
            try data.write(to: url, options: .atomic)
            exportFailed = false
            exportMessage = "诊断包已保存，没有上传。"
        } catch {
            exportFailed = true
            exportMessage = error.localizedDescription
        }
    }
}
