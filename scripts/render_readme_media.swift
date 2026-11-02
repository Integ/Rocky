// AppKit renderer for the README's illustrative product tour, not app screenshots.
import AppKit
import Foundation

let arguments = CommandLine.arguments
guard arguments.count == 3 else { fatalError("Usage: swift render_readme_media.swift OUTPUT ROOT") }
let output = URL(fileURLWithPath: arguments[1], isDirectory: true)
let root = URL(fileURLWithPath: arguments[2], isDirectory: true)
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
struct Scene: Decodable {
    let heading: String
    let description: String
    let caption: String
}
let scenes = try JSONDecoder().decode([Scene].self, from: Data(contentsOf: root.appendingPathComponent("scripts/readme_storyboard.json")))
precondition(scenes.count == 8)

func color(_ hex: UInt32) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255,
            green: CGFloat((hex >> 8) & 255) / 255, blue: CGFloat(hex & 255) / 255, alpha: 1)
}
let ink = color(0x163D39), muted = color(0x57726C), teal = color(0x087F7B)
let mint = color(0xE0F4EF), line = color(0xD8E7E2), white = NSColor.white
let icon = NSImage(contentsOf: root.appendingPathComponent("desktop/Branding/Rocky-Obsidian.png"))!

func box(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat,
         fill: NSColor = .white, radius: CGFloat = 18, stroke: NSColor? = nil) {
    let path = NSBezierPath(roundedRect: NSRect(x: x, y: y, width: w, height: h),
                            xRadius: radius, yRadius: radius)
    fill.setFill(); path.fill()
    if let stroke { stroke.setStroke(); path.lineWidth = 1.5; path.stroke() }
}

func text(_ value: String, _ x: CGFloat, _ y: CGFloat, _ width: CGFloat,
          size: CGFloat = 26, weight: NSFont.Weight = .regular, tint: NSColor = ink,
          height: CGFloat = 90, align: NSTextAlignment = .left) {
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = align
    paragraph.lineBreakMode = .byWordWrapping
    paragraph.lineSpacing = size * 0.17
    let string = NSAttributedString(string: value, attributes: [
        .font: NSFont.systemFont(ofSize: size, weight: weight),
        .foregroundColor: tint, .paragraphStyle: paragraph,
    ])
    let needed = string.boundingRect(with: NSSize(width: width, height: 2000),
                                     options: [.usesLineFragmentOrigin, .usesFontLeading]).height
    precondition(needed <= height + 2, "Text overflows: \(value) (\(needed) > \(height))")
    string.draw(in: NSRect(x: x, y: y, width: width, height: height))
}

func pill(_ value: String, _ x: CGFloat, _ y: CGFloat, _ width: CGFloat,
          fill: NSColor = mint, tint: NSColor = teal) {
    box(x, y, width, 42, fill: fill, radius: 21)
    text(value, x + 10, y + 8, width - 20, size: 19, weight: .medium, tint: tint,
         height: 30, align: .center)
}

func button(_ value: String, _ x: CGFloat, _ y: CGFloat, _ width: CGFloat, primary: Bool = true) {
    box(x, y, width, 54, fill: primary ? teal : white, radius: 12, stroke: primary ? nil : line)
    text(value, x + 10, y + 13, width - 20, size: 22, weight: .semibold,
         tint: primary ? white : ink, height: 33, align: .center)
}

func label(_ number: String, _ title: String, _ detail: String, _ y: CGFloat,
           x: CGFloat = 804, width: CGFloat = 968, selected: Bool = false) {
    box(x, y, width, 106, fill: selected ? mint : white, radius: 16, stroke: selected ? nil : line)
    box(x + 20, y + 25, 52, 52, fill: selected ? teal : mint, radius: 26)
    text(number, x + 24, y + 35, 44, size: 23, weight: .semibold,
         tint: selected ? white : teal, height: 34, align: .center)
    text(title, x + 94, y + 20, width - 120, size: 25, weight: .semibold, height: 39)
    text(detail, x + 94, y + 60, width - 120, size: 20, tint: muted, height: 32)
}

func file(_ title: String, _ detail: String, _ x: CGFloat, _ y: CGFloat, _ width: CGFloat) {
    box(x, y, width, 88, fill: color(0xF5F9F7), radius: 12, stroke: line)
    box(x + 16, y + 20, 38, 48, fill: mint, radius: 7)
    text("↗", x + 21, y + 25, 30, size: 25, tint: teal, height: 40)
    text(title, x + 72, y + 17, width - 90, size: 23, weight: .medium, height: 35)
    text(detail, x + 72, y + 53, width - 90, size: 18, tint: muted, height: 28)
}

func window(_ title: String) {
    box(760, 218, 1064, 660, fill: color(0xE6EEEA), radius: 27)
    box(760, 208, 1064, 660, fill: white, radius: 26, stroke: line)
    for (i, c) in [color(0xEE857A), color(0xE8C266), color(0x7ABF9E)].enumerated() {
        box(786 + CGFloat(i) * 23, 232, 13, 13, fill: c, radius: 7)
    }
    text(title, 930, 226, 700, size: 20, weight: .medium, tint: muted, height: 32, align: .center)
    box(784, 266, 1016, 1, fill: line, radius: 0)
}

func overview() {
    window("Rocky · 从目标到交付")
    box(784, 284, 220, 560, fill: color(0xF3F7F5), radius: 14)
    text("最近任务", 808, 306, 170, size: 19, weight: .medium, tint: muted, height: 30)
    box(798, 355, 192, 70, fill: mint, radius: 12)
    text("整理发布资料", 816, 368, 165, size: 21, weight: .semibold, height: 34)
    text("交付待审阅", 816, 400, 165, size: 15, tint: teal, height: 25)
    text("每周资料汇总\n\n更新项目说明", 816, 454, 164, size: 20, tint: muted, height: 130)
    text("待你处理     2\n\n议程\n\n设置与模型", 816, 650, 164, size: 20, weight: .medium, height: 170)
    text("整理项目发布资料", 1036, 301, 710, size: 31, weight: .semibold, height: 47)
    pill("交付待审阅", 1036, 361, 160)
    text("资料已整理，文件已核验并保存快照。", 1036, 427, 716, size: 23, height: 40)
    file("release-notes.md", "更新摘要与升级说明", 1036, 486, 716)
    file("sources.csv", "来源清单与核对记录", 1036, 590, 716)
    text("执行完成 ≠ 你已接受", 1036, 704, 716, size: 21, tint: muted, height: 34)
    button("接受结果", 1036, 762, 180)
    button("要求修改…", 1234, 762, 180, primary: false)
    text("查看交付历史 →", 1464, 776, 290, size: 21, tint: teal, height: 33)
}

func contract() {
    window("01 · 确认任务要求")
    text("整理项目发布资料", 806, 296, 930, size: 31, weight: .semibold, height: 48)
    text("验收标准", 806, 362, 900, size: 21, weight: .medium, tint: muted, height: 34)
    for (i, value) in ["说明本次功能与修复", "标注来源与适用范围", "列出安装方式和升级限制"].enumerated() {
        text("✓", 812, 420 + CGFloat(i) * 48, 35, size: 26, tint: teal, height: 40)
        text(value, 856, 420 + CGFloat(i) * 48, 850, size: 25, height: 40)
    }
    text("交付文件", 806, 592, 400, size: 21, weight: .medium, tint: muted, height: 35)
    pill("release-notes.md", 806, 644, 250)
    pill("sources.csv", 1074, 644, 204)
    pill("100 步", 806, 722, 150)
    pill("50 万 tokens", 974, 722, 208)
    pill("1 小时", 1200, 722, 148)
    button("确认并开始", 1508, 770, 264)
}

func execution() {
    window("02 · 持续执行")
    pill("后台服务正在运行", 806, 292, 260)
    label("✓", "读取参考资料", "保留实际工具结果与来源", 358)
    label("✓", "生成交付文件", "文件工具、网页、HTTP 与脚本", 478)
    label("✓", "独立核验", "核对文件状态、事实与证据引用", 598)
    label("↻", "保存执行检查点", "等待、反馈或重启后继续", 718, selected: true)
}

func attention() {
    window("03 · 待你处理")
    box(806, 292, 970, 204, fill: color(0xF5F9F7), stroke: line)
    pill("需要操作批准", 828, 310, 206)
    text("运行生成报告的脚本", 828, 370, 900, size: 28, weight: .semibold, height: 45)
    text("python build_report.py", 828, 428, 540, size: 23, tint: muted, height: 38)
    button("批准此操作", 1534, 420, 214)
    box(806, 516, 970, 230, fill: mint)
    text("需要你的回复", 830, 538, 900, size: 21, weight: .medium, tint: teal, height: 34)
    text("这份报告主要面向哪类读者？", 830, 590, 900, size: 29, weight: .semibold, height: 48)
    button("产品团队", 830, 662, 200, primary: false)
    button("技术团队", 1050, 662, 200, primary: false)
    text("补充消息：已保存 → 已送达 → 已纳入执行", 810, 789, 960, size: 23, tint: muted, height: 38)
}

func agenda() {
    window("04 · 议程")
    pill("长期目标", 806, 294, 170)
    pill("定时任务", 994, 294, 170)
    pill("事件任务", 1182, 294, 170)
    label("1", "长期目标 · 发布产品资料", "拆成步骤，前一步的交付传给下一步", 378, selected: true)
    label("2", "定时任务 · 每周整理资料", "一次、间隔、每天或每周", 504)
    label("3", "事件任务 · 参考文件变化", "默认仅观察，确认后可开启自动执行", 630)
    text("自动启动有每日额度；旧事件不会在次日补跑。", 814, 790, 940, size: 23, tint: muted, height: 40)
}

func delivery() {
    window("05 · 交付审阅与历史")
    pill("第 1 轮", 806, 292, 150, fill: color(0xEFF3F1), tint: muted)
    pill("第 2 轮 · 待审阅", 976, 292, 252)
    text("本轮交付已保存", 806, 373, 940, size: 33, weight: .semibold, height: 51)
    text("任务要求 · 结果总结 · 独立验收反馈 · 文件快照", 806, 440, 950, size: 23, tint: muted, height: 40)
    file("release-notes.md", "当前文件变化，也不会覆盖历史原件", 806, 504, 970)
    file("sources.csv", "预览与导出时重新核对完整性", 806, 608, 970)
    button("接受结果", 806, 766, 228)
    button("要求修改…", 1054, 766, 228, primary: false)
    text("在原任务上继续 →", 1388, 781, 390, size: 22, tint: teal, height: 37)
}

func transparency() {
    window("06 · 执行诊断、提示词与运维")
    let cards: [(String, String, String)] = [
        ("模型调度", "4 / 1", "默认总并发 / 人工预留"),
        ("执行诊断", "状态 + 预算", "排队、调用、工具与重试"),
        ("提示词版本", "9 个模块", "按任务记录指令快照"),
        ("空间占用", "6 类统计", "工作区、快照、数据库等"),
        ("本地诊断", "脱敏 ZIP", "不含任务内容或凭据"),
        ("运行维护", "备份与恢复", "服务状态、日志和令牌"),
    ]
    for (i, item) in cards.enumerated() {
        let x = CGFloat(806 + (i % 3) * 328), y = CGFloat(300 + (i / 3) * 260)
        box(x, y, 314, 234, fill: i == 4 ? mint : color(0xF5F9F7), radius: 16, stroke: line)
        text(item.0, x + 20, y + 23, 274, size: 23, weight: .medium, tint: muted, height: 37)
        text(item.1, x + 20, y + 86, 274, size: 31, weight: .semibold, tint: teal, height: 54)
        text(item.2, x + 20, y + 166, 274, size: 18, tint: muted, height: 55)
    }
}

func closing() {
    box(760, 208, 1064, 660, fill: ink, radius: 26)
    icon.draw(in: NSRect(x: 1194, y: 274, width: 190, height: 190), from: .zero,
              operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil)
    text("Rocky v0.2", 820, 492, 944, size: 56, weight: .semibold, tint: white, height: 82, align: .center)
    text("原生 macOS · 本机持久化 · 多模型", 820, 598, 944, size: 28,
         tint: color(0xC3DFD7), height: 48, align: .center)
    pill("下载 v0.2.0", 1152, 684, 280, fill: mint, tint: ink)
    text("github.com/Integ/Rocky", 820, 772, 944, size: 27,
         tint: white, height: 45, align: .center)
}

let drawScenes: [() -> Void] = [overview, contract, execution, attention, agenda, delivery, transparency, closing]
for index in 0..<8 {
    autoreleasepool {
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1920, pixelsHigh: 1080,
                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                     isPlanar: false, colorSpaceName: .deviceRGB,
                                     bytesPerRow: 0, bitsPerPixel: 0)!
        let base = NSGraphicsContext(bitmapImageRep: bitmap)!
        base.cgContext.translateBy(x: 0, y: 1080)
        base.cgContext.scaleBy(x: 1, y: -1)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(cgContext: base.cgContext, flipped: true)
        box(0, 0, 1920, 1080, fill: color(0xF2F7F4), radius: 0)
        box(1400, -380, 900, 900, fill: color(0xE5F2EB), radius: 450)
        icon.draw(in: NSRect(x: 88, y: 64, width: 76, height: 76), from: .zero,
                  operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil)
        text("Rocky", 180, 78, 500, size: 43, weight: .semibold, height: 65)
        pill("v0.2.0", 1636, 82, 188, fill: white, tint: teal)
        text(index == 0 ? "持续执行 · 人工交接 · 版本化交付" : String(format: "%02d / 08", index + 1), 100, 218, 600, size: 22,
             weight: .medium, tint: teal, height: 37)
        text(scenes[index].heading, 96, 300, 632, size: index == 0 ? 78 : 63,
             weight: .semibold, height: 218)
        text(scenes[index].description, 100, 546, 618, size: 27, tint: muted, height: 132)
        let notes: [String]
        switch index {
        case 0: notes = ["持续执行与中断恢复", "长期目标、定时与事件", "每轮核验与人工审阅"]
        case 1: notes = ["附加参考文件或授权文件夹", "预算可调整，有限制也可不限"]
        case 2: notes = ["Mac 需保持唤醒", "合盖或手动睡眠时暂停，唤醒后继续"]
        case 3: notes = ["批准绑定具体操作", "提问与回复不等于工具授权"]
        case 4: notes = ["按已确认的固定任务要求执行", "文件与外部事件只作为参考资料"]
        case 5: notes = ["每轮保存核验过的文件原件", "明确接受，或追加修改要求"]
        case 6: notes = ["人工请求优先获得调用名额", "诊断包由你决定是否分享"]
        default: notes = ["macOS 14+ · Apple Silicon 下载包", "云端或本机模型，取决于你的配置"]
        }
        for (i, note) in notes.enumerated() {
            box(101, 706 + CGFloat(i) * 54, 7, 7, fill: teal, radius: 4)
            text(note, 125, 694 + CGFloat(i) * 54, 584, size: 22, height: 38)
        }
        drawScenes[index]()
        for i in 0..<8 {
            box(760 + CGFloat(i) * 135, 894, 119, 5, fill: i == index ? teal : line, radius: 3)
        }
        box(96, 934, 1728, 72, fill: ink, radius: 16)
        text(scenes[index].caption, 124, 953, 1672, size: 28, weight: .medium,
             tint: white, height: 44, align: .center)
        text("功能示意 · 示例数据 · 非实际任务记录", 100, 1031, 1150,
             size: 18, tint: muted, height: 30)
        text("github.com/Integ/Rocky", 1350, 1031, 474, size: 18,
             tint: muted, height: 30, align: .right)
        NSGraphicsContext.restoreGraphicsState()
        let data = bitmap.representation(using: .png, properties: [:])!
        try! data.write(to: output.appendingPathComponent(String(format: "scene-%02d.png", index)))
    }
}
print("Rendered 8 illustrative scenes at 1920×1080.")
