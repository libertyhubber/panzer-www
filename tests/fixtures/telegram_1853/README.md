# Chroma rounding-bias and 57-day date-offset regression

Manually verified pair supplied by the repository owner. Both images are
680 × 606 pixels. Preserve their original JPEG bytes.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2021/11/2021-11-27_3B64D3C0.jpg>
  - Filename date: `2021-11-27`
  - 98,920 bytes; copied from the sibling archive and verified byte-for-byte
    against the public archive URL
  - SHA-256: `c395225403e460efbd550e8f40476f18c4a1e6ffd9f07951e78f331e744b78dc`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/1853>
  - Post date: `2022-01-23T01:12:55+00:00` (57 days after the archive date)
  - Largest still-image variant (`x`), 116,406 bytes; no video variants
  - Copied from the backfill's disk cache; Telegram API verification confirmed
    the selected variant and its cache path
  - SHA-256: `24f58293458194bdb4f855ce0d8658a29ef7815ab678122dfb49b4df96104248`
  - Legacy image digest: `4b2b25492000`, different from the archive's digest
    (`6fbd6e494000`), despite matching image content

The date window and photo-size selection are correct. At 128 × 128 preview
resolution, this pair fails only the old chroma mean-shift gate in the YCbCr
fallback:

- Maximum RGB RMS: approximately 3.941 (above 3.0)
- Luminance RMS: approximately 1.633 (below 2.0)
- Maximum chroma RMS: approximately 1.661 (below 3.25)
- Luminance outlier fraction (>8): zero
- Maximum chroma outlier fraction (>12): zero
- Absolute signed mean shifts: Y approximately 1.058, Cb approximately 1.135,
  Cr approximately 0.796

Allowing up to 1.25 levels of chroma mean bias accepts this pair. Luminance mean,
RMS, outlier, aspect-ratio, explicit visual opt-in and ambiguity checks remain
unchanged. Live verification found exactly one passing match among all 1,640
archive candidates within ±90 days of the post. The coarse index also retained
this unique candidate and returned the same match.

Offline tests cover the old rejection, visual opt-in, duplicate ambiguity,
caption removal, the minimum ±57-day window, indexed matching, the independent
chroma mean boundary, automatic replanning of an exhausted ±90-day scan,
disk-cache reuse and metadata export using the actual post date. No real message
cache, backfill state or gallery metadata was modified during verification.
