---
name: texture-styles
description: Add the iPhone 18 "Texture" Photographic Styles controls (Soft Skin, Glow, Film, Grain) to HEIC photos taken on older iPhones, by generating semantic mattes locally and injecting them into the HEIF container. Use when the user wants Texture/Photographic Styles 3 on a photo from an iPhone 17 or earlier, asks why Texture is missing, or wants to batch-process HEIC files for it.
---

# Texture Styles for older iPhone photos

Photos decides whether to show the Texture controls by reading the **file**, not
by checking the device. An iPhone 18 photo carries 12 semantic mattes plus a
small metadata item; older iPhones don't write them, so the controls never
appear. Add both and the controls show up — verified on iPhone 17 Pro Max / iOS 27.

This skill generates the mattes from the user's own photo (via macOS Vision) so
the masks line up with the pixels, then injects them without re-encoding any
pixel data.

## Prerequisites

Check these before promising anything:

- **macOS only** — uses Apple's Vision framework. Not available on Linux/Windows,
  and cannot run on a hosted server.
- `ffmpeg` — `brew install ffmpeg`
- Xcode Command Line Tools — `xcode-select --install`
- The Swift helper must be built once:

      swiftc -O <repo>/tools/gen/genmattes.swift -o <repo>/tools/gen/genmattes

If a prerequisite is missing, say which one and give the install command. Do not
attempt a workaround — there isn't one.

## Usage

Always `check` before `inject` when the user's file might not qualify:

    python3 tools/texture_cli.py check  <in.HEIC>
    python3 tools/texture_cli.py inject <in.HEIC> [-o out.HEIC]

Add `--json` for machine-readable output when batching.

Exit codes: `0` success · `2` file rejected · `3` processing failed ·
`4` missing prerequisite.

## Which files work

| | |
|---|---|
| **Best** | HEIC straight from an iPhone, never edited or exported, **with a visible face** |
| OK | An exported/edited HEIC — `check` warns when grid dimensions disagree with EXIF |
| **Won't work** | JPEG, PNG, anything sent through LINE/Messenger/iCloud Link (re-encoded), photos already from an iPhone 18 |

The file must reach the Mac by **AirDrop or USB**. Messaging apps re-encode HEIC
and strip the item structure this depends on — a re-encoded file will be
rejected at `check` with a clear reason.

## Reporting results honestly

Two things to tell the user rather than let them discover later:

**No face in the photo.** `inject` reports `faces: 0`. Film and Grain operate on
the whole frame and still work; **Soft Skin is matte-driven and will do nothing**.
Landscape, food and object photos fall here. Say so instead of implying the
photo gained the full feature.

**Sending the result back.** AirDrop only. If the user sends it through a chat
app it will be re-encoded and the mattes lost, and the Texture controls will
silently not appear — which looks like the tool failed.

Also: Vision has no landmarks for glasses, tattoos, ears or hands, so those four
mattes are written empty. Apple's own files carry near-empty mattes for some of
these, so Photos accepts it.

## Batch processing

    for f in *.HEIC; do
      python3 tools/texture_cli.py inject "$f" --json
    done

Each file is independent; a rejection (exit 2) is not a failure of the run.
Summarise at the end how many were processed, how many were skipped and why,
and how many had no face detected.

## What not to claim

- This does **not** require an iPhone 18, and does not make a photo "become" one.
- Faking the EXIF model string to "iPhone 18 Pro Max" does nothing — tested.
  Don't offer it as a fallback.
- Photos validates that matte dimensions match the primary image's aspect ratio,
  but does **not** check that the mask content matches the pixels.
- Pixel data is copied byte-for-byte; image quality is unchanged. The original
  file is never modified — output goes to a new file.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `not a readable HEIF/HEIC` | JPEG/PNG, or a re-encoded copy from a chat app |
| `already has a texture_styles item` | Already an iPhone 18 photo |
| `primary item is 'hvc1'` | Single-tile HEIF; only grid-based images are supported |
| Processed fine but no controls on the phone | Sent through a re-encoding channel — re-send by AirDrop |
| `exit 4` | ffmpeg missing, or `genmattes` not built (see Prerequisites) |
