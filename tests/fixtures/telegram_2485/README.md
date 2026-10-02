# January 2022 archive / Telegram 2485 color-text-edge regression

Manually verified by the repository owner. Preserve original JPEG bytes.
Both images are 500 × 649 pixels.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2022/01/2022-01-22_2B98D2AB.jpg>
  - 91,701 bytes; copied from the sibling archive and compared byte-for-byte
    with the public archive URL
  - SHA-256: `843960dd24497fe48ad2ca7b3989c6d96179a604f70970605fa5e7491796a020`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/2485>
  - Post date: `2022-03-11T21:06:21+00:00`, 48 days after the archive filename date
  - Largest still variant `x`, 89,605 bytes; no video variants
  - Selected size and existing disk-cache path verified through the Telegram API
  - SHA-256: `70b528fc01c784d6cc65852ef0f6c6ea3b63b57f8a793e1b98643c9895d6ecc0`
  - Legacy digest: `690788865bab` (archive: `4d058865d9a3`)

At 128px preview resolution, maximum RGB RMS is 5.697, luminance RMS is
1.359, and maximum chroma RMS is 2.820. Luminance outliers (>8) are zero;
maximum chroma outlier fraction (>12) is 0.012207 (old limit: 0.01).
Absolute signed mean shifts are Y 0.690, Cb 0.046 and Cr 0.878.

The larger differences concentrate around colored text edges. Under a
1px Gaussian chroma blur, maximum RMS falls to 1.809 and outlier fraction
to zero. Raw outliers outside chroma structure shared by both originals
occupy 0.000122 of the Cb channel (under 0.002). This passes the guarded
edge branch, while the unchanged luminance outlier gate preserves text checks.
Local recoloring without shared original color structure remains rejected.

Live verification found exactly one passing archive candidate among 1,991
within ±90 days of the Telegram post. Indexed matching gives the same result.
The minimum date window is ±48 days, not the default ±7 days.

Offline tests cover all four January 2022 pairs: prior rejection, opt-in,
duplicate ambiguity, minimum date windows, indexed/full-scan equivalence,
caption removal, automatic replanning, cached-photo reuse and metadata export.
No real message mappings, backfill state or gallery metadata were modified
while collecting/verifying these fixtures.
