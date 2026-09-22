# Technical findings

Reverse-engineering notes on how Photos gates Photographic Styles 3 (Texture).
All results are from device testing on iPhone 17 Pro Max / iOS 27, September 2026.

## The two metadata items

Apple ships Photographic Styles data in HEIF items of type `uri `.

**Styles 2** (present since iPhone 15-ish):

    tag:apple.com,2023:photo:metadata:styles

~57 KB bplist. Keys `0`-`7` and `c`-`l`. Contains tone-curve statistics
(`LinearImage*`, `ToneMappedImage*` blocks with p02/p10/p25/p50/p75/p98 +
whitePoint/blackPoint/highKey), `PeopleRatio`, `SkinRatio`, a 51,840-byte blob
that decodes as 2,160 3x4 colour matrices, and two 2,048-byte LUTs.

**No Texture parameters anywhere.** Scanned all 75 distinct ASCII tokens — no
`textur`, `grain`, `sharp`, `detail` or `micro`.

**Styles 3 / Texture** (iPhone 18):

    tag:apple.com,2026:photo:metadata:texture_styles

Only ~213-224 bytes, plain bplist, no blobs:

| key | example |
|---|---|
| `CaptureMode` | `Still` |
| `CaptureType` | `LF` (main) / `DF` (telephoto) |
| `FilmGrainSeed` | 277 / 269 |
| `HardwareModel` | `iPhone19,7` |
| `PortType` | `PortTypeBack` / `PortTypeBackTelephoto` |
| `Preset` | `Soft` |
| `TextureStylePeopleDataVersion` | 3 |

The 2023 item is nearly identical between 17 and 18 — same 17 keys, only new
key `l` (bool). So Styles 2 was not extended; Texture got its own container.

## The aux mattes

The real differentiator. iPhone 18 writes 12 mattes the 17 does not, all tagged
`tag:apple.com,2026:photo:aux:`:

    semanticnosematte          semanticskinmattev2
    semanticnonfaceskinmatte   semanticlipsmatte
    semanticteethmattev2       semanticpersonmatte
    semanticglassesmattev2     semanticeyebrowsmatte
    semantictattoomatte        semantichandsmatte
    semanticearsmatte          semanticfaceskinmatte

Each is an `hvc1` item, 768x576 for a 4:3 photo, sharing one `hvcC`/`pixi`/`irot`,
each with its own `auxC`, plus a paired `mime` sidecar linked by `cdsc`.

iPhone 17 Pro Max carries only the older set: `hdrgainmap`, `semanticskymatte`,
`linearthumbnail`, `styledeltamap` (and on selfies, the 2018-2020 `urn:` mattes).

## Test matrix

All tests on the same base photo (IMG_0345, iPhone 17 Pro Max camera original),
viewed on a 17 Pro Max.

| # | mattes | texture item | styles payload | EXIF model | result |
|---|---|---|---|---|---|
| J | – | – | 17 | 17 | works, old panel |
| S1 | – | – | 17 | **18** | works, old panel |
| S2 | – | – | **18** | **18** | works, old panel |
| S3 | – | ✓ | 18 | 18 | **style controls dead** |
| S4 | – | ✓ | 17 | 18 | **style controls dead** |
| T3 | ✓ 12 | – | 17 | 17 | no Texture |
| T1 | ✓ 12 | ✓ | 17 | 17 | **Texture slider** |
| T2 | ✓ 12 | ✓ | 17 | **18** | **Texture slider** |
| T4 | ✓ 12 | ✓ | **18** | **18** | **Texture slider** |

### What this proves

**EXIF identity is irrelevant.** S1/S2 claim to be an iPhone 18 Pro Max in
Model, HostComputer and LensModel and behave exactly like the untouched file.
T1 works while still identifying as a 17. This is why earlier exiftool-based
attempts could never have succeeded — exiftool cannot write HEIF items at all,
so it was editing the wrong layer.

**The 2023 styles payload is irrelevant.** S2 carries the 18's full
57,986-byte payload on a 17's photo and still yields the old panel.

**Both the mattes and the item are required.** Either alone fails, and the item
without mattes is actively destructive: Photos discards the entire style
metadata set rather than degrading, killing colour controls that worked before.

## The aspect-ratio check

Transplanting mattes into arbitrary targets failed across the board:

| file | primary | ratio | matte ispe | ratio | result |
|---|---|---|---|---|---|
| T1 | 5712x4284 | 1.3333 | 768x576 | 1.3333 | works |
| U3 | 2316x3088 | 0.7500 | 768x576 | 1.3333 | fails |
| U1 | 1200x751 | 1.5979 | 768x576 | 1.3333 | fails |

The donor mattes are 4:3; T1 only worked because its base photo is also 4:3.
U3 is instructive — a genuine 17 Pro Max camera original with *more* aux mattes
than T1, failing purely because it is portrait.

**So Photos validates matte geometry against the primary image**, but does *not*
validate that mask content matches the pixels (T1 uses a completely different
scene's masks and still works).

## Implementation notes

Things that bit us, recorded so they don't bite again.

**`construction_method`.** Apple's `grid` and `tmap` items use
`construction_method=1`, meaning extent offsets are relative to the `idat` box,
not the file. Treating them as file offsets silently reads the wrong bytes.

**`altr` group ids share the item-id space.** A gap in the item numbering is
usually an `EntityToGroup` id in `grpl`, not a missing item. New item ids must
clear `max(item_id, max_group_id)`.

**Item ordering.** Real Apple files place metadata items adjacently with the
Exif item **last**. Appending after Exif is legal per spec but matches no real
file. (Not proven to matter, but cheap to match.)

**`ipma` property indices are per-file.** Copying items between files requires
remapping every property index into the target's `ipco`, not just copying the
entries.

**EXIF Orientation.** A photo with Orientation 6 stores a landscape buffer that
displays as portrait. Vision operates on the displayed image, so generated masks
must be rotated back into storage orientation or they land 90° off.

**Vision landmarks are y-up, and so is CGContext.** Flipping rows when writing
the mask file double-flips it, producing upside-down faces that still look
plausible in a histogram. Decode and *look* at generated masks.

## Verification approach

Every output was checked with:

- an independent parser (not just the one that wrote the file)
- macOS ImageIO (`sips`) and libheif (`pillow_heif`) — two separate decoders
- byte-identity of every pre-existing item, proving no pixel re-encoding
- decoded PNG hash comparison before/after injection
- iref preservation (gain map and tmap survive)
- visual inspection of decoded mattes — this is what caught the y-flip bug
