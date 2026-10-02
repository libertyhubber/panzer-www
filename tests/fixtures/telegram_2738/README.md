# Shared-chroma-edge outlier regression: Telegram 2738

Manually verified by the repository owner. Preserve original JPEG bytes.
Both images are 624 × 422 pixels.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2022/02/2022-02-14_0FC8C158.jpg>
  - 80,246 bytes; copied from the sibling archive and verified byte-for-byte
    against the public URL
  - SHA-256: `89eb2a8dc9ea276dc0aeba3d584db1dbc393b00f72eb4baa02c875bf47e72fb9`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/2738>
  - Post date: `2022-03-28T21:29:04+00:00` (42-day offset)
  - Largest still variant `x`, 76,458 bytes; no video variants
  - Telegram API verification confirmed selected variant and existing cache path
  - SHA-256: `3d8a8d7ae3d7e644db90b7409994e09624156d4a1564b268dc656c3da53fa2fe`
  - Legacy digest: `00064bd15a9a` (archive: `00084cf56ce5`)

At 128px preview resolution:

- Maximum RGB RMS: 6.506
- Luminance RMS: 1.543
- Maximum chroma RMS: 3.244
- Luminance outlier fraction (>8): 0.000061
- Maximum raw chroma outlier fraction (>12): 0.023315
- Absolute signed mean shifts: Y 0.753, Cb 0.097, Cr 0.809
- After 1px Gaussian chroma blur: maximum RMS 2.335; outlier fraction 0.001648
- Raw Cb outliers outside shared chroma structure: 33 / 16,384 = 0.002014

Version 9 rejects only that final flat-region outlier budget: 33 pixels exceed
the old 0.2% allowance (32 pixels). A 0.25% allowance admits up to 40 pixels.
The raw chroma outlier ceiling, smoothing checks, shared-structure definition,
RMS and mean-shift limits are unchanged. Larger local recoloring remains rejected.

Live verification found exactly one passing archive match among 1,991 candidates
within ±90 days of the actual post date. The coarse index gives the same result.
The minimum date window is ±42 days.

Offline tests cover the specific rejection gate, visual opt-in, duplicate
ambiguity, date-window boundaries, caption removal, outlier-budget boundaries,
automatic replanning, cached-photo reuse and metadata export. No real mappings,
backfill state or gallery metadata were modified during verification.
