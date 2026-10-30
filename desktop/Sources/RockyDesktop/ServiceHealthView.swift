import AppKit
import SwiftUI

struct ServiceStatus: Decodable, Equatable {
    struct Worker: Decodable, Equatable {
        var state: String
        var temporal: Bool?
    }
    var status: String
    var worker: Worker
    var tasks: [String: Int]
    var pending_approvals: Int?
    var disk_free_bytes: Int64?
    var problems: [String]
}

struct BackupItem: Decodable, Equatable, Identifiable {
    var name: String
    var path: String
    var bytes: Int64
    var created_at: String
    var id: String { path }
}

struct BackupList: Decodable, Equatable {
    var directory: String
    var interval_hours: Double
    var keep: Int
    var backups: [BackupItem]
}

extension APIClient {
    func serviceStatus() async throws -> ServiceStatus { try await request("/status") }
    func backups() async throws -> BackupList { try await request("/backups") }
    func createBackup() async throws -> BackupItem { try await request("/backups", method: "POST", timeout: 900) }
}

/// Service health and backups in Settings.
struct ServiceHealthSection: View {
    @ObservedObject var model: AppModel
    @State private var status: ServiceStatus?
    @State private var backups: BackupList?
    @State private var working = false
    @State private var message: String?

    var body: some View {
        Section {
            if let status {
                Label(status.problems.isEmpty ? "运行正常" : status.problems.joined(separator: "；"),
                      systemImage: status.problems.isEmpty ? "checkmark.circle" : "exclamationmark.triangle")
                    .foregroundStyle(status.problems.isEmpty ? Color.green : Color.orange)
                Text(summary(status)).font(.system(size: 11)).foregroundStyle(.secondary)
            } else {
                Text("正在读取服务状态…").foregroundStyle(.secondary)
            }
            if let latest = backups?.backups.first {
                Text("最近备份：\(Self.date(latest.created_at))，\(Self.size(latest.bytes))，共 \(backups?.backups.count ?? 0) 份")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            } else if backups != nil {
                Text("还没有备份。").font(.system(size: 11)).foregroundStyle(.secondary)
            }
            HStack(spacing: 10) {
                Button(working ? "正在备份…" : "立即备份") { Task { await backUp() } }.disabled(working)
                Button("打开备份文件夹") {
                    if let directory = backups?.directory { NSWorkspace.shared.open(URL(fileURLWithPath: directory)) }
                }.disabled(backups == nil)
                Spacer()
                Button("刷新") { Task { await load() } }
            }
            if let message { Text(message).font(.system(size: 11)).foregroundStyle(.secondary) }
        } header: { Text("运行状态与备份") } footer: {
            Text(footer)
        }
        .task { await load() }
    }

    private var footer: String {
        let schedule = (backups?.interval_hours ?? 24) > 0
            ? "后台服务每 \(Int(backups?.interval_hours ?? 24)) 小时自动备份数据库和任务文件夹，保留最近 \(backups?.keep ?? 7) 份。"
            : "自动备份已关闭。"
        return schedule + "备份不含 API 密钥（在钥匙串里）。恢复时先停止后台服务，再在终端运行 rocky restore <备份文件>，当前数据会先移到一旁保留。"
    }

    private func summary(_ status: ServiceStatus) -> String {
        let worker = switch status.worker.state {
        case "up": status.worker.temporal == false ? "执行进程在运行，执行引擎未连上" : "执行进程在运行"
        case "stale": "执行进程没有响应"
        case "down": "执行进程未运行"
        default: "执行进程状态未知"
        }
        let active = ["queued", "running", "waiting", "blocked"].reduce(0) { $0 + (status.tasks[$1] ?? 0) }
        var parts = [worker, "进行中任务 \(active) 个"]
        if let approvals = status.pending_approvals { parts.append("待审批 \(approvals) 项") }
        if let free = status.disk_free_bytes { parts.append("磁盘剩余 \(Self.size(free))") }
        return parts.joined(separator: " · ")
    }

    private func load() async {
        status = try? await model.api.serviceStatus()
        backups = try? await model.api.backups()
    }

    private func backUp() async {
        working = true
        defer { working = false }
        do {
            let item = try await model.api.createBackup()
            message = "已备份：\(item.name)"
        } catch {
            message = error.localizedDescription
        }
        await load()
    }

    static func size(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    static func date(_ value: String) -> String {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let fallback = ISO8601DateFormatter()
        guard let date = parser.date(from: value) ?? fallback.date(from: value) else { return value }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
}
