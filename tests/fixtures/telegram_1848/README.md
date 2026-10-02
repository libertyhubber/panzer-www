# Luminance rounding-bias and 57-day date-offset regression

Manually verified pair supplied by the repository owner. Both full-size images
are 578 × 1280 pixels. Preserve their original JPEG bytes.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2021/11/2021-11-26_8766DFD4.jpg>
  - Filename date: `2021-11-26`
  - 110,394 bytes; copied from the sibling archive and verified byte-for-byte
    against the public archive URL
  - SHA-256: `38154a3a378054c6a560e8fdf62e3f03952b6b0fbb8fe74f962f65e1ef69de4d`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/1848>
  - Post date: `2022-01-22T02:23:36+00:00` (57 days after the archive date)
  - Largest still-image variant (`y`), 129,880 bytes; no video variants
  - Copied from the backfill's disk cache; Telegram API verification confirmed
    the selected variant and its cache path
  - SHA-256: `ee6425585b4674623527b46494fb479ced12a728e027b1c438fa927447a49ad3`
  - Legacy image digest: `d6455a088290`, matching the message cache

The public Telegram embed serves the smaller `x` variant (361 × 800 pixels,
73,794 bytes), not the full-size photo used by the backfill. The actual selected
photo fails only the old channel-mean gate in the YCbCr fallback:

- Maximum RGB RMS: approximately 3.618 (above 3.0)
- Luminance RMS: approximately 1.765 (below 2.0)
- Maximum chroma RMS: approximately 1.362 (below 3.25)
- Luminance outlier fraction (>8): approximately 0.000305 (below 0.001)
- Maximum chroma outlier fraction (>12): zero
- Absolute signed mean shifts: Y approximately 1.388, Cb approximately 0.934,
  Cr approximately 0.943

Allowing up to 1.5 levels of luminance mean bias, while retaining the 1.0-level
chroma mean limit, accepts this pair. RMS, outlier, aspect-ratio, explicit visual
opt-in and ambiguity checks remain unchanged. Live verification found exactly
one passing match among all 1,629 archive candidates within ±90 days of the post.
The coarse index also retained this unique candidate and returned the same match.

Offline tests cover the old rejection, visual opt-in, duplicate ambiguity,
caption removal, the minimum ±57-day window, indexed matching, the independent
luminance mean boundary, automatic replanning of an exhausted ±90-day scan,
disk-cache reuse and metadata export using the actual post date. No real message
cache, backfill state or gallery metadata was modified during verification.
