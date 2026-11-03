// Code-rendered v0.3 product illustrations. All task data is synthetic.
import AppKit
import Foundation

let arguments = CommandLine.arguments
guard arguments.count == 3 else { fatalError("Usage: swift render_readme_media.swift OUTPUT ROOT") }
let output = URL(fileURLWithPath: arguments[1], isDirectory: true)
let root = URL(fileURLWithPath: arguments[2], isDirectory: true)
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
struct Scene: Decodable { let heading: String; let description: String; let caption: String }
let scenes = try JSONDecoder().decode([Scene].self, from: Data(contentsOf: root.appendingPathComponent("scripts/readme_storyboard.json")))
precondition(scenes.count == 8)

func color(_ hex: UInt32) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255,
            green: CGFloat((hex >> 8) & 255) / 255, blue: CGFloat(hex & 255) / 255, alpha: 1)
}
let ink = color(0x181818), muted = color(0x686868), soft = color(0xF4F4F4)
let line = color(0xDFDFDF), white = NSColor.white
let icon = NSImage(contentsOf: root.appendingPathComponent("desktop/Branding/Rocky-Obsidian.png"))!

func box(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat,
         fill: NSColor = .white, radius: CGFloat = 18, stroke: NSColor? = nil) {
    let path = NSBezierPath(roundedRect: NSRect(x: x, y: y, width: w, height: h), xRadius: radius, yRadius: radius)
    fill.setFill(); path.fill()
    if let stroke { stroke.setStroke(); path.lineWidth = 1.5; path.stroke() }
}
func text(_ value: String, _ x: CGFloat, _ y: CGFloat, _ width: CGFloat,
          size: CGFloat = 26, weight: NSFont.Weight = .regular, tint: NSColor = ink,
          height: CGFloat = 90, align: NSTextAlignment = .left, mono: Bool = false) {
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = align; paragraph.lineBreakMode = .byWordWrapping; paragraph.lineSpacing = size * 0.17
    let font = mono ? NSFont.monospacedSystemFont(ofSize: size, weight: weight) : NSFont.systemFont(ofSize: size, weight: weight)
    let string = NSAttributedString(string: value, attributes: [.font: font, .foregroundColor: tint, .paragraphStyle: paragraph])
    let needed = string.boundingRect(with: NSSize(width: width, height: 2000), options: [.usesLineFragmentOrigin, .usesFontLeading]).height
    precondition(needed <= height + 2, "Text overflows: \(value) (\(needed) > \(height))")
    string.draw(in: NSRect(x: x, y: y, width: width, height: height))
}
func pill(_ value: String, _ x: CGFloat, _ y: CGFloat, _ width: CGFloat, dark: Bool = false) {
    box(x, y, width, 42, fill: dark ? ink : soft, radius: 21)
    text(value, x + 12, y + 8, width - 24, size: 19, weight: .medium, tint: dark ? white : ink, height: 30, align: .center)
}
func button(_ value: String, _ x: CGFloat, _ y: CGFloat, _ width: CGFloat, primary: Bool = true) {
    box(x, y, width, 54, fill: primary ? ink : white, radius: 27, stroke: primary ? nil : line)
    text(value, x + 12, y + 13, width - 24, size: 22, weight: .semibold, tint: primary ? white : ink, height: 33, align: .center)
}
func window(_ title: String) {
    box(764, 216, 1060, 660, fill: color(0xE6E6E6), radius: 27)
    box(760, 208, 1064, 660, fill: white, radius: 26, stroke: line)
    for i in 0..<3 { box(786 + CGFloat(i) * 23, 232, 13, 13, fill: color(0xC6C6C6), radius: 7) }
    text(title, 940, 226, 690, size: 20, weight: .medium, tint: muted, height: 32, align: .center)
    box(784, 266, 1016, 1, fill: line, radius: 0)
}
func row(_ marker: String, _ title: String, _ detail: String, _ y: CGFloat, selected: Bool = false) {
    box(806, y, 970, 102, fill: selected ? soft : white, radius: 16, stroke: selected ? nil : line)
    box(826, y + 25, 50, 50, fill: selected ? ink : soft, radius: 25)
    text(marker, 830, y + 33, 42, size: 23, weight: .medium, tint: selected ? white : ink, height: 36, align: .center)
    text(title, 900, y + 17, 850, size: 25, weight: .semibold, height: 40)
    text(detail, 900, y + 59, 850, size: 20, tint: muted, height: 32)
}
func file(_ name: String, _ detail: String, _ y: CGFloat) {
    box(806, y, 970, 90, fill: soft, radius: 14)
    text("↗", 828, y + 24, 44, size: 26, height: 42)
    text(name, 896, y + 16, 848, size: 24, weight: .medium, height: 37)
    text(detail, 896, y + 54, 848, size: 19, tint: muted, height: 30)
}
func overview() {
    window("Rocky · 新任务")
    box(784, 284, 222, 560, fill: soft, radius: 14)
    text("任务", 808, 310, 170, size: 20, weight: .semibold, height: 32)
    box(798, 364, 194, 62, fill: color(0xE5E5E5), radius: 12)
    text("＋  新建任务", 816, 381, 162, size: 21, weight: .medium, height: 36)
    text("整理发布资料\n\n核对项目说明\n\n每周资料汇总", 816, 462, 164, size: 20, tint: muted, height: 170)
    text("待你处理\n\n议程\n\n设置与模型", 816, 677, 164, size: 19, weight: .medium, height: 150)
    text("今天想完成什么？", 1040, 352, 708, size: 39, weight: .semibold, height: 60, align: .center)
    text("交给 Rocky，从规划到交付。", 1040, 424, 708, size: 23, tint: muted, height: 42, align: .center)
    box(1040, 512, 708, 166, fill: soft, radius: 28)
    text("整理项目资料，生成一份带来源的报告。", 1066, 538, 648, size: 25, height: 74)
    text("＋", 1066, 628, 45, size: 29, tint: muted, height: 40)
    box(1674, 614, 52, 52, fill: ink, radius: 26)
    text("↑", 1683, 621, 34, size: 28, weight: .medium, tint: white, height: 42, align: .center)
    text("参考文件 · 授权文件夹 · 自定执行预算", 1044, 717, 700, size: 21, tint: muted, height: 37, align: .center)
    pill("查找证据", 1060, 780, 196)
    pill("批量修改", 1280, 780, 196)
    pill("核验交付", 1500, 780, 196)
}
func contract() {
    window("01 · 确认任务要求")
    text("整理项目发布资料", 806, 298, 930, size: 32, weight: .semibold, height: 50)
    text("验收标准", 806, 370, 900, size: 21, weight: .medium, tint: muted, height: 34)
    for (i, value) in ["说明本次功能与修复", "记录来源、路径和适用范围", "列出安装方式和升级说明"].enumerated() {
        text("✓", 812, 422 + CGFloat(i) * 48, 35, size: 26, height: 40)
        text(value, 856, 422 + CGFloat(i) * 48, 850, size: 25, height: 40)
    }
    text("交付文件", 806, 602, 900, size: 21, weight: .medium, tint: muted, height: 34)
    pill("report.md", 806, 650, 204)
    pill("sources.csv", 1028, 650, 204)
    text("示例预算", 806, 734, 250, size: 20, tint: muted, height: 32)
    pill("100 步", 806, 780, 150)
    pill("50 万 tokens", 974, 780, 208)
    button("确认并开始", 1506, 768, 270)
}
func tools() {
    window("02 · 查找文件与搜索内容")
    pill("查找文件", 806, 294, 184, dark: true)
    text("**/*release*.txt", 1012, 302, 735, size: 24, height: 38, mono: true)
    file("releases/release-alpha.txt", "匹配文件名，返回相对路径与文件大小", 370)
    file("releases/release-beta.txt", "缩小范围，继续搜索文件内容", 476)
    box(806, 600, 970, 174, fill: ink, radius: 17)
    text("搜索内容  ·  发布负责人", 830, 620, 910, size: 23, weight: .medium, tint: white, height: 40)
    text("release-alpha.txt : 2   发布负责人：林秋\nrelease-beta.txt  : 3   发布负责人：周宁", 830, 674, 910, size: 23, tint: color(0xE0E0E0), height: 86, mono: true)
    text("路径 + 行号 + 上下文；支持分页，明确提示截断。", 812, 800, 950, size: 22, tint: muted, height: 36)
}
func batches() {
    window("03 · 独立文件批次")
    pill("同一轮规划", 806, 294, 200, dark: true)
    text("示例：依次修改 4 个独立文件", 1030, 300, 730, size: 25, weight: .medium, height: 42)
    let items = [("✓", "README.md", "已保存 · 独立回执"), ("✓", "notes/brief.md", "已保存 · 独立回执"), ("✓", "notes/checklist.md", "已保存 · 独立回执"), ("✓", "notes/decisions.md", "已保存 · 独立回执")]
    for (i, item) in items.enumerated() { row(item.0, item.1, item.2, 368 + CGFloat(i) * 106, selected: i == 3) }
    text("最多 8 个工作区文件；新输入、审批或不确定结果会停止后续修改。", 812, 808, 954, size: 21, tint: muted, height: 35)
}
func attention() {
    window("04 · 待你处理")
    box(806, 294, 970, 194, fill: soft, radius: 18)
    text("需要操作批准", 830, 315, 900, size: 21, weight: .medium, tint: muted, height: 36)
    text("运行生成报告的脚本", 830, 366, 900, size: 28, weight: .semibold, height: 46)
    text("python build_report.py", 830, 428, 630, size: 22, tint: muted, height: 38, mono: true)
    button("批准此操作", 1536, 418, 212)
    box(806, 510, 970, 240, fill: white, radius: 18, stroke: line)
    text("需要你的回复", 830, 533, 900, size: 21, weight: .medium, tint: muted, height: 36)
    text("这份报告主要面向哪类读者？", 830, 588, 900, size: 29, weight: .semibold, height: 48)
    button("产品团队", 830, 674, 200, primary: false)
    button("技术团队", 1048, 674, 200, primary: false)
    text("补充消息：已保存 → 已送达 → 已纳入执行", 812, 800, 950, size: 23, tint: muted, height: 38)
}
func delivery() {
    window("05 · 交付审阅与历史")
    pill("第 1 轮", 806, 294, 150)
    pill("第 2 轮 · 待审阅", 974, 294, 252, dark: true)
    text("本轮交付已保存", 806, 376, 940, size: 34, weight: .semibold, height: 54)
    text("任务要求 · 结果总结 · 独立验收 · 文件快照", 806, 444, 950, size: 23, tint: muted, height: 40)
    file("report.md", "当前文件变化，也不会覆盖历史原件", 512)
    file("sources.csv", "预览与导出时重新核对完整性", 618)
    button("接受结果", 806, 774, 228)
    button("要求修改…", 1054, 774, 228, primary: false)
    text("在原任务上继续 →", 1406, 788, 370, size: 22, tint: muted, height: 38)
}
func execution() {
    window("06 · 后台执行与计划")
    let cards: [(String, String, String)] = [
        ("后台服务", "关窗继续", "等待、回复、重启后续做"),
        ("议程", "按计划推进", "长期目标、定时与事件"),
        ("执行记录", "实际工具结果", "来源、回执和检查点"),
        ("执行预算", "看清余量", "步骤、调用、tokens、时间"),
        ("模型调度", "人工优先", "模型调用与排队可查看"),
        ("本地运维", "备份与诊断", "存储统计与脱敏导出"),
    ]
    for (i, item) in cards.enumerated() {
        let x = CGFloat(806 + (i % 3) * 328), y = CGFloat(302 + (i / 3) * 244)
        box(x, y, 314, 220, fill: soft, radius: 17)
        text(item.0, x + 20, y + 22, 274, size: 22, weight: .medium, tint: muted, height: 36)
        text(item.1, x + 20, y + 82, 274, size: 29, weight: .semibold, height: 50)
        text(item.2, x + 20, y + 154, 274, size: 18, tint: muted, height: 50)
    }
    text("Mac 需保持唤醒；合盖或手动睡眠时暂停，唤醒后继续。", 812, 800, 950, size: 21, tint: muted, height: 36)
}
func closing() {
    box(760, 208, 1064, 660, fill: ink, radius: 26)
    icon.draw(in: NSRect(x: 1204, y: 274, width: 172, height: 172), from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil)
    text("Rocky v0.3", 820, 490, 944, size: 58, weight: .semibold, tint: white, height: 88, align: .center)
    text("原生 macOS · 本机持久化 · 多模型", 820, 602, 944, size: 27, tint: color(0xD0D0D0), height: 48, align: .center)
    button("下载 v0.3.0", 1148, 686, 288, primary: false)
    text("github.com/Integ/Rocky", 820, 784, 944, size: 26, tint: white, height: 44, align: .center)
}

let drawScenes: [() -> Void] = [overview, contract, tools, batches, attention, delivery, execution, closing]
let notes = [
    ["查找证据 · 批量修改 · 独立验收", "工作数据保存在这台 Mac 上"],
    ["附加参考文件或授权文件夹", "预算可以调整"],
    ["11 项内置工具", "搜索位置可用于定向读取"],
    ["逐文件保存进展", "部分失败保留已完成文件"],
    ["批准绑定具体操作", "用户回复保留消费回执"],
    ["每轮保留核验过的文件原件", "审阅后接受，或继续修改"],
    ["定时与事件任务按设置启动", "状态、预算和实际操作可追踪"],
    ["Apple Silicon · macOS 14+", "云端或本机模型，按你的配置"],
]
for index in 0..<8 {
    autoreleasepool {
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1920, pixelsHigh: 1080, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        let base = NSGraphicsContext(bitmapImageRep: bitmap)!
        base.cgContext.translateBy(x: 0, y: 1080); base.cgContext.scaleBy(x: 1, y: -1)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(cgContext: base.cgContext, flipped: true)
        box(0, 0, 1920, 1080, fill: color(0xF7F7F7), radius: 0)
        icon.draw(in: NSRect(x: 88, y: 64, width: 76, height: 76), from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil)
        text("Rocky", 180, 78, 500, size: 43, weight: .semibold, height: 65)
        pill("v0.3.0", 1636, 82, 188, dark: true)
        text(index == 0 ? "原生 macOS 任务执行助手" : String(format: "%02d / 08", index + 1), 100, 218, 600, size: 22, weight: .medium, tint: muted, height: 37)
        text(scenes[index].heading, 96, 300, 632, size: index == 0 ? 78 : 63, weight: .semibold, height: 218)
        text(scenes[index].description, 100, 546, 618, size: 27, tint: muted, height: 132)
        for (i, note) in notes[index].enumerated() {
            box(101, 712 + CGFloat(i) * 54, 7, 7, fill: ink, radius: 4)
            text(note, 125, 700 + CGFloat(i) * 54, 584, size: 22, height: 38)
        }
        drawScenes[index]()
        for i in 0..<8 { box(760 + CGFloat(i) * 135, 894, 119, 5, fill: i == index ? ink : line, radius: 3) }
        box(96, 934, 1728, 72, fill: ink, radius: 16)
        text(scenes[index].caption, 124, 953, 1672, size: 28, weight: .medium, tint: white, height: 44, align: .center)
        text("功能示意 · 合成数据", 100, 1031, 1150, size: 18, tint: muted, height: 30)
        text("github.com/Integ/Rocky", 1350, 1031, 474, size: 18, tint: muted, height: 30, align: .right)
        NSGraphicsContext.restoreGraphicsState()
        try! bitmap.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent(String(format: "scene-%02d.png", index)))
    }
}
print("Rendered 8 neutral v0.3 product illustrations at 1920×1080.")
