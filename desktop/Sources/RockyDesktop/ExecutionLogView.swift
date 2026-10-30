import SwiftUI

struct ExecutionLogGroup: Identifiable {
    var entries: [ExecutionEntry]
    var id: Int { entries[0].id }
    var title: String { entries[0].title }
    var stepLabel: String { entries[0].stepLabel }
    var failed: Bool { entries.contains { $0.observation?.success == false || ["failed", "budget_exhausted", "deadline_exceeded"].contains($0.status) } }
    var statusLabel: String {
        let results = entries.compactMap(\.observation)
        guard !results.isEmpty else { return entries[0].statusLabel }
        if results.allSatisfy(\.success) { return "已完成" }
        return results.contains(where: \.success) ? "部分失败" : "执行失败"
    }

    static func build(_ entries: [ExecutionEntry]) -> [ExecutionLogGroup] {
        let observedReasons = Set(entries.filter { $0.observation != nil }.map(\.reason))
        var groups: [ExecutionLogGroup] = []
        for entry in entries {
            // Keep the raw history intact; omit redundant running checkpoints
            // only in this presentation. Pauses, errors and artifact changes stay.
            if entry.observation == nil, entry.status == "running", entry.displayReason == nil,
               !entry.artifactsChanged, entry.wakeAt == nil, observedReasons.contains(entry.reason) { continue }
            if let step = entry.firstStep, let last = groups.last,
               last.entries[0].firstStep == step, last.entries[0].reason == entry.reason,
               last.entries.allSatisfy({ $0.observation != nil }), entry.observation != nil {
                groups[groups.count - 1].entries.append(entry)
            } else {
                groups.append(ExecutionLogGroup(entries: [entry]))
            }
        }
        return groups
    }
}

struct ExecutionOutputSection: Identifiable {
    var title: String
    var text: String
    var id: String { title }

    static func parse(_ text: String) -> [ExecutionOutputSection] {
        if let stdout = text.range(of: "\nstdout:\n"),
           let stderr = text.range(of: "\nstderr:\n", range: stdout.upperBound..<text.endIndex) {
            return [
                Self(title: "执行状态", text: String(text[..<stdout.lowerBound])),
                Self(title: "标准输出", text: String(text[stdout.upperBound..<stderr.lowerBound])),
                Self(title: "错误输出", text: String(text[stderr.upperBound...])),
            ].filter { !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        }
        if let data = text.data(using: .utf8),
           let object = try? JSONSerialization.jsonObject(with: data) {
            if var metadata = object as? [String: Any] {
                var sections: [Self] = []
                for (key, title) in [("stdout", "标准输出"), ("stderr", "错误输出"), ("body", "响应正文"), ("text", "文本内容"), ("content", "返回内容")] {
                    if let value = metadata[key] as? String {
                        metadata.removeValue(forKey: key)
                        if !value.isEmpty { sections.append(Self(title: title, text: value)) }
                    }
                }
                if !sections.isEmpty {
                    if !metadata.isEmpty, let formatted = try? JSONSerialization.data(withJSONObject: metadata, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
                       let value = String(data: formatted, encoding: .utf8) {
                        sections.insert(Self(title: "响应信息", text: value), at: 0)
                    }
                    return sections
                }
            }
            if let formatted = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
               let json = String(data: formatted, encoding: .utf8) {
                return [Self(title: "结构化结果", text: json)]
            }
        }
        return [Self(title: "结果", text: text)]
    }
}

extension ToolObservation {
    var outputSummary: String {
        if !success {
            let code = content.split(separator: "\n").first.map(String.init) ?? ""
            switch code {
            case "path_not_found": return "路径不存在，需要调整路径"
            case "approval_required": return "等待操作授权"
            default: return String(code.prefix(120))
            }
        }
        switch tool {
        case "read_file": return "已读取文件内容"
        case "list_files": return "已列出目录内容"
        case "run_command":
            let first = content.split(separator: "\n").first.map(String.init) ?? "命令已完成"
            return first.hasPrefix("Exit code: ") ? "退出码：" + first.dropFirst("Exit code: ".count) : first
        case "http_request", "fetch_url":
            if let data = content.data(using: .utf8), let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let status = value["status_code"] as? Int { return "HTTP \(status)" }
            return "已取得响应内容"
        case "browse":
            if let data = content.data(using: .utf8), let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let title = value["title"] as? String, !title.isEmpty { return "已打开：\(title.prefix(80))" }
            return "已打开网页"
        default: return "操作已完成"
        }
    }
}

struct ExecutionLogView: View {
    let entries: [ExecutionEntry]

    var body: some View {
        let groups = ExecutionLogGroup.build(entries)
        VStack(alignment: .leading, spacing: 10) {
            ForEach(groups) { group in
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 8) {
                        Image(systemName: group.failed ? "exclamationmark.circle" : "circle.fill")
                            .foregroundStyle(group.failed ? Color.orange : Color.secondary)
                            .font(.system(size: group.failed ? 12 : 5))
                        Text(group.stepLabel).font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
                        Spacer()
                        Text((group.entries.count > 1 ? "\(group.entries.count) 项操作 · " : "") + group.statusLabel)
                            .font(.system(size: 10)).foregroundStyle(.tertiary)
                    }
                    Text(group.title).font(.system(size: 12)).lineSpacing(4).textSelection(.enabled)
                    ForEach(group.entries) { entry in
                        if let observation = entry.observation {
                            DisclosureGroup {
                                output(observation.content)
                            } label: {
                                HStack(alignment: .top, spacing: 7) {
                                    Image(systemName: observation.success ? "checkmark.circle" : "exclamationmark.circle")
                                        .foregroundStyle(observation.success ? Color.secondary : Color.orange)
                                    Text(observation.toolLabel).fontWeight(.medium)
                                    Text(observation.outputSummary).foregroundStyle(.secondary).lineLimit(2)
                                    if entry.count > 1 { Text("×\(entry.count)").foregroundStyle(.tertiary) }
                                }.font(.system(size: 11))
                            }
                        }
                        if entry.artifactsChanged {
                            Label(entry.artifacts.isEmpty ? "产物列表已更新" : entry.artifacts.map(\.path).joined(separator: "、"), systemImage: "doc")
                                .font(.system(size: 10)).foregroundStyle(.secondary)
                        }
                        if let wakeAt = entry.wakeAt, entry.status == "waiting" {
                            Text("计划恢复：" + wakeTime(wakeAt)).font(.system(size: 10)).foregroundStyle(.secondary)
                        }
                    }
                }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.primary.opacity(0.025), in: RoundedRectangle(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.primary.opacity(0.07)))
            }
        }.padding(.top, 10)
    }

    private func output(_ text: String) -> some View {
        ScrollView([.horizontal, .vertical]) {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(ExecutionOutputSection.parse(text)) { section in
                    VStack(alignment: .leading, spacing: 5) {
                        Text(section.title).font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
                        Text(section.text).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                    }
                }
                if text.isEmpty { Text("没有返回文本内容").font(.system(size: 11)).foregroundStyle(.secondary) }
            }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
        }.frame(maxHeight: 220).background(Color.primary.opacity(0.03), in: RoundedRectangle(cornerRadius: 7))
            .padding(.top, 6)
    }

    private func wakeTime(_ value: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = formatter.date(from: value) ?? ISO8601DateFormatter().date(from: value)
        return date?.formatted(date: .abbreviated, time: .shortened) ?? value
    }
}
