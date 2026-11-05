// xjframegrab —— 用系统 AVFoundation 拆视频帧，输出 PNG
// 用法: xjframegrab <video> <outDir> [step=1] [--crop x,y,w,h]
// 说明: 本机 ffmpeg 是 x86 版（Bad CPU type），故用系统框架拆帧。
import Foundation
import AVFoundation
import AppKit
import VideoToolbox

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

var args = CommandLine.arguments
guard args.count >= 3 else { fail("usage: xjframegrab <video> <outDir> [step] [--crop x,y,w,h]") }

let videoPath = args[1]
let outPath = args[2]
var step = 1
var cropRect: CGRect? = nil
var i = 3
while i < args.count {
    if args[i] == "--crop", i + 1 < args.count {
        let p = args[i + 1].split(separator: ",").compactMap { Double($0) }
        if p.count == 4 { cropRect = CGRect(x: p[0], y: p[1], width: p[2], height: p[3]) }
        i += 2
    } else {
        if let s = Int(args[i]), s > 0 { step = s }
        i += 1
    }
}

let url = URL(fileURLWithPath: videoPath)
let outURL = URL(fileURLWithPath: outPath)
try? FileManager.default.createDirectory(at: outURL, withIntermediateDirectories: true)
try? FileManager.default.removeItem(at: outURL.appendingPathComponent("frames"))
try? FileManager.default.createDirectory(at: outURL.appendingPathComponent("frames"), withIntermediateDirectories: true)
let framesDir = outURL.appendingPathComponent("frames")

let asset = AVURLAsset(url: url)
guard let track = asset.tracks(withMediaType: .video).first else { fail("no video track") }
let dur = CMTimeGetSeconds(asset.duration)
print("fps=\(String(format: "%.3f", track.nominalFrameRate)) size=\(Int(track.naturalSize.width))x\(Int(track.naturalSize.height)) duration=\(String(format: "%.3f", dur))")

guard let reader = try? AVAssetReader(asset: asset) else { fail("reader init failed") }
let settings: [String: Any] = [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
]
let output = AVAssetReaderTrackOutput(track: track, outputSettings: settings)
output.alwaysCopiesSampleData = false
guard reader.canAdd(output) else { fail("cannot add output") }
reader.add(output)
guard reader.startReading() else { fail("startReading failed: \(reader.error?.localizedDescription ?? "?")") }

var idx = 0
var written = 0
while let sb = output.copyNextSampleBuffer() {
    defer { idx += 1 }
    if idx % step != 0 { continue }
    guard let pb = CMSampleBufferGetImageBuffer(sb) else { continue }
    var cg: CGImage?
    VTCreateCGImageFromCVPixelBuffer(pb, options: nil, imageOut: &cg)
    guard var img = cg else { continue }
    if let r = cropRect {
        // 裁切，坐标以左上为原点
        let w = CGFloat(img.width), h = CGFloat(img.height)
        let rect = CGRect(x: r.origin.x, y: h - r.origin.y - r.height, width: r.width, height: r.height)
        if let cropped = img.cropping(to: rect) { img = cropped }
        _ = w
    }
    let rep = NSBitmapImageRep(cgImage: img)
    if let data = rep.representation(using: .png, properties: [:]) {
        let name = String(format: "frame-%04d.png", written)
        try? data.write(to: framesDir.appendingPathComponent(name))
        written += 1
    }
}
print("decoded=\(idx) written=\(written) status=\(reader.status.rawValue)")
