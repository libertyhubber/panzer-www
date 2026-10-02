# Luminance-outlier regression: Telegram 2025

Manually verified by the repository owner. Preserve original JPEG bytes.
Both images are 690 × 500 pixels.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2021/12/2021-12-12_38EE955A.jpg>
  - 124,523 bytes; copied from the sibling archive and verified byte-for-byte
    against the public URL
  - SHA-256: `bde20e985719de1747510aadfbefa474635e1d416a9da80992063731c1e59e01`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/2025>
  - Post date: `2022-02-06T11:11:51+00:00` (56-day offset)
  - Largest still variant `x`, 141,374 bytes; no video variants
  - Telegram API verification confirmed selected variant and existing cache path
  - SHA-256: `70c85310d5a650b94940b69e3f4c46c5fdd7ca8cfc305e412fe5f02910fd933a`
  - Legacy digest: `2004d3d358db` (same as archive; exact bytes/pixels differ)

At 128px preview resolution:

- Maximum RGB RMS: 5.021
- Luminance RMS: 2.236 (within 2.25)
- Maximum chroma RMS: 2.354 (within 3.75)
- Luminance outliers (>8): 22 / 16,384 = 0.001343
- Maximum chroma outlier fraction (>12): 0.011230
- Absolute signed mean shifts: Y 1.490, Cb 0.013, Cr 0.270

Version 9 rejected only the luminance outlier fraction: 22 pixels exceed the
old 0.1% allowance (16 pixels). A 0.15% allowance admits up to 24 pixels.
The existing shared-color-edge check already passes for the chroma artifacts.
RMS, mean-shift, aspect-ratio and other edge checks are unchanged.

Live verification found exactly one passing archive match among 1,794 candidates
within ±90 days of the actual post date. The coarse index gives the same result.
The minimum date window is ±56 days.

Offline tests cover the specific rejection gate, visual opt-in, duplicate
ambiguity, date-window boundaries, caption removal, outlier-budget boundaries,
automatic replanning, cached-photo reuse and metadata export. No real mappings,
backfill state or gallery metadata were modified during verification.
