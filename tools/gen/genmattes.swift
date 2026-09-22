// genmattes.swift — generate semantic mattes for an image using Apple's Vision
// framework, the same family of segmentation Photos itself uses.
//
// Emits one 8-bit grayscale PGM per matte at a caller-specified size.
// Usage: genmattes <input-image> <outdir> <width> <height>

import Foundation
import Vision
import CoreImage
import AppKit

let args = CommandLine.arguments
guard args.count >= 5,
      let W = Int(args[3]), let H = Int(args[4]) else {
    FileHandle.standardError.write("usage: genmattes <img> <outdir> <w> <h>\n".data(using:.utf8)!)
    exit(2)
}
let inPath = args[1], outDir = args[2]
try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)

guard let img = NSImage(contentsOfFile: inPath),
      let tiff = img.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let cg = rep.cgImage else {
    FileHandle.standardError.write("cannot load image\n".data(using:.utf8)!); exit(1)
}

let ciCtx = CIContext(options: [.useSoftwareRenderer: false])

/// Resample a single-channel CVPixelBuffer to WxH and write as PGM.
func writePGM(_ pb: CVPixelBuffer, _ name: String) {
    CVPixelBufferLockBaseAddress(pb, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
    let sw = CVPixelBufferGetWidth(pb), sh = CVPixelBufferGetHeight(pb)
    let stride = CVPixelBufferGetBytesPerRow(pb)
    let fmt = CVPixelBufferGetPixelFormatType(pb)
    guard let base = CVPixelBufferGetBaseAddress(pb) else { return }
    let src = base.assumingMemoryBound(to: UInt8.self)
    let isFloat = (fmt == kCVPixelFormatType_OneComponent32Float)

    var out = [UInt8](repeating: 0, count: W*H)
    for y in 0..<H {
        let sy = min(sh-1, y * sh / H)
        for x in 0..<W {
            let sx = min(sw-1, x * sw / W)
            var v: UInt8 = 0
            if isFloat {
                let f = src.advanced(by: sy*stride + sx*4).withMemoryRebound(to: Float.self, capacity: 1) { $0.pointee }
                v = UInt8(max(0, min(255, f * 255)))
            } else {
                v = src[sy*stride + sx]
            }
            out[y*W + x] = v
        }
    }
    var data = "P5\n\(W) \(H)\n255\n".data(using: .ascii)!
    data.append(contentsOf: out)
    try? data.write(to: URL(fileURLWithPath: "\(outDir)/\(name).pgm"))
    FileHandle.standardError.write("  \(name): \(sw)x\(sh) -> \(W)x\(H)\n".data(using:.utf8)!)
}

func writeBlank(_ name: String, _ value: UInt8 = 0) {
    var data = "P5\n\(W) \(H)\n255\n".data(using: .ascii)!
    data.append(contentsOf: [UInt8](repeating: value, count: W*H))
    try? data.write(to: URL(fileURLWithPath: "\(outDir)/\(name).pgm"))
    FileHandle.standardError.write("  \(name): blank\n".data(using:.utf8)!)
}

let handler = VNImageRequestHandler(cgImage: cg, options: [:])

// ---- person segmentation (accurate) -> semanticpersonmatte ----
var personMask: CVPixelBuffer? = nil
if #available(macOS 12.0, *) {
    let req = VNGeneratePersonSegmentationRequest()
    req.qualityLevel = .accurate
    req.outputPixelFormat = kCVPixelFormatType_OneComponent8
    do {
        try handler.perform([req])
        if let r = req.results?.first { personMask = r.pixelBuffer }
    } catch { FileHandle.standardError.write("person seg failed: \(error)\n".data(using:.utf8)!) }
}
if let pm = personMask { writePGM(pm, "semanticpersonmatte") } else { writeBlank("semanticpersonmatte") }

// ---- face landmarks -> derive face-region mattes ----
var faceObs: [VNFaceObservation] = []
let fReq = VNDetectFaceLandmarksRequest()
do { try handler.perform([fReq]); faceObs = fReq.results ?? [] }
catch { FileHandle.standardError.write("face req failed: \(error)\n".data(using:.utf8)!) }
FileHandle.standardError.write("faces: \(faceObs.count)\n".data(using:.utf8)!)

/// Rasterise a set of normalised polygons into a WxH mask.
func rasterise(_ polys: [[CGPoint]], _ name: String) {
    guard !polys.isEmpty else { writeBlank(name); return }
    let cs = CGColorSpaceCreateDeviceGray()
    guard let ctx = CGContext(data: nil, width: W, height: H, bitsPerComponent: 8,
                              bytesPerRow: W, space: cs,
                              bitmapInfo: CGImageAlphaInfo.none.rawValue) else { writeBlank(name); return }
    ctx.setFillColor(CGColor(gray: 0, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))
    ctx.setFillColor(CGColor(gray: 1, alpha: 1))
    for p in polys where p.count > 2 {
        ctx.beginPath()
        ctx.move(to: CGPoint(x: p[0].x * CGFloat(W), y: p[0].y * CGFloat(H)))
        for q in p.dropFirst() { ctx.addLine(to: CGPoint(x: q.x * CGFloat(W), y: q.y * CGFloat(H))) }
        ctx.closePath(); ctx.fillPath()
    }
    guard let data = ctx.data else { writeBlank(name); return }
    let buf = data.assumingMemoryBound(to: UInt8.self)
    var out = Data("P5\n\(W) \(H)\n255\n".data(using: .ascii)!)
    // Vision landmarks are y-up and CGContext is y-up, so the raster already
    // matches image orientation once read top-down.
    for y in 0..<H {
        out.append(contentsOf: UnsafeBufferPointer(start: buf.advanced(by: y*W), count: W))
    }
    try? out.write(to: URL(fileURLWithPath: "\(outDir)/\(name).pgm"))
    FileHandle.standardError.write("  \(name): \(polys.count) region(s)\n".data(using:.utf8)!)
}

func region(_ f: VNFaceObservation, _ kp: (VNFaceLandmarks2D) -> VNFaceLandmarkRegion2D?) -> [CGPoint]? {
    guard let lm = f.landmarks, let r = kp(lm) else { return nil }
    let bb = f.boundingBox
    return (0..<r.pointCount).map { i -> CGPoint in
        let p = r.normalizedPoints[i]
        return CGPoint(x: bb.origin.x + CGFloat(p.x) * bb.width,
                       y: bb.origin.y + CGFloat(p.y) * bb.height)
    }
}

rasterise(faceObs.compactMap { region($0) { $0.outerLips } }, "semanticlipsmatte")
rasterise(faceObs.compactMap { region($0) { $0.innerLips } }, "semanticteethmattev2")
rasterise(faceObs.compactMap { region($0) { $0.nose } }, "semanticnosematte")
rasterise(faceObs.compactMap { region($0) { $0.leftEyebrow } }
        + faceObs.compactMap { region($0) { $0.rightEyebrow } }, "semanticeyebrowsmatte")
rasterise(faceObs.compactMap { region($0) { $0.faceContour } }, "semanticfaceskinmatte")

// glasses: Vision has no glasses landmark -> blank (Photos tolerates empty mattes,
// the donor's own glasses matte is 156B i.e. effectively empty)
writeBlank("semanticglassesmattev2")
writeBlank("semantictattoomatte")
writeBlank("semanticearsmatte")

// hands: use person mask minus face region as a coarse proxy is unreliable; leave blank
writeBlank("semantichandsmatte")

// skin v2 = person mask restricted to skin-ish areas; approximate with person mask
if let pm = personMask { writePGM(pm, "semanticskinmattev2") } else { writeBlank("semanticskinmattev2") }
// non-face skin = person minus face contour
rasterise(faceObs.compactMap { region($0) { $0.faceContour } }, "__facetmp")
if let pm = personMask {
    writePGM(pm, "semanticnonfaceskinmatte")
} else { writeBlank("semanticnonfaceskinmatte") }

FileHandle.standardError.write("done\n".data(using:.utf8)!)
