# JPEG recompression visual-match regression

Manually verified pair supplied by the repository owner. Both images are
624 × 482 pixels. Preserve their original JPEG bytes.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2023/11/2023-11-06_9CFC1942.jpg>
  - Filename date: `2023-11-06`
  - 59,212 bytes; copied from the sibling archive and verified byte-for-byte
    against the public archive URL
  - SHA-256: `d510ecd10391f177db86410a698eb3e3aa4d0e654de1947b9e4602d6a4ea60c7`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/10029>
  - Post date: `2023-11-04T16:02:40+00:00`
  - Largest still-image variant (`x`), 77,748 bytes; no video variants
  - Copied from the backfill's disk cache after verifying the current photo and
    selected variant through Telegram's API
  - SHA-256: `586df9bb1c88d07709c1b629c8247006d84ce345824c4fef9503a8d57bf185b0`
  - Legacy image digest: `26d890cd08f4`, matching the message cache

The two-day archive lag is already covered by the default date window. The
photo size selection is also correct. The old visual matcher rejected both
RMS gates in its YCbCr fallback:

- Maximum RGB RMS: approximately 5.599
- Luminance RMS: approximately 1.698 (old limit: 1.5)
- Maximum chroma RMS: approximately 3.014 (old limit: 3.0)
- Luminance outlier fraction (>8): zero
- Maximum chroma outlier fraction (>12): approximately 0.008179 (under 1%)
- Maximum absolute signed YCbCr mean shift: approximately 0.800 (under 1.0)

Luminance/chroma RMS limits of 2.0/3.25 admit this recompression. All RGB,
aspect-ratio, outlier, mean-shift, visual opt-in and ambiguity gates remain
unchanged. Live archive-window verification found exactly one passing match
among 165 candidates within ±7 days, and among 1,991 candidates within ±90 days.

Offline tests cover the original rejection gates, opt-in, duplicate ambiguity,
caption changes, automatic replanning of an exhausted search, disk-cache reuse
and metadata export using the actual post date. No real message cache, backfill
state or gallery metadata was modified when collecting/verifying the fixture.
