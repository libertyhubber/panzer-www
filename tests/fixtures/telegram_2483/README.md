# January 2022 archive / Telegram 2483 color-text-edge regression

Manually verified by the repository owner. Preserve original JPEG bytes.
Both images are 570 × 500 pixels.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2022/01/2022-01-22_4C737E9E.jpg>
  - 56,123 bytes; copied from the sibling archive and compared byte-for-byte
    with the public archive URL
  - SHA-256: `d8e2f29ead70b825cde76b151839a738b38f028137ffb73a43ee22bae8710c91`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/2483>
  - Post date: `2022-03-11T21:06:20+00:00`, 48 days after the archive filename date
  - Largest still variant `x`, 56,598 bytes; no video variants
  - Selected size and existing disk-cache path verified through the Telegram API
  - SHA-256: `9dd4fb69a9d5e638f85f50b5b1fb9c1d58aa68a7c9c82b62b949f8bdccbcee3a`
  - Legacy digest: `75b9ab61200c` (archive: `75b97361200c`)

At 128px preview resolution, maximum RGB RMS is 7.327, luminance RMS is
1.519, and maximum chroma RMS is 3.611. Luminance outliers (>8) are zero;
maximum chroma outlier fraction (>12) is 0.030273 (old limit: 0.01).
Absolute signed mean shifts are Y 0.724, Cb 0.108 and Cr 0.859.

The larger differences concentrate around colored text edges. Under a
1px Gaussian chroma blur, maximum RMS falls to 2.481 and outlier fraction
to 0.000305. Raw outliers outside chroma structure shared by both originals
occupy 0.001587 of the Cb channel (under 0.002). This passes the guarded
edge branch, while the unchanged luminance outlier gate preserves text checks.
Simply raising the raw chroma outlier limit would also admit local recoloring;
offline tests explicitly demonstrate why smoothing alone is insufficient.

Live verification found exactly one passing archive candidate among 1,991
within ±90 days of the Telegram post. Indexed matching gives the same result.
The minimum date window is ±48 days, not the default ±7 days.

Offline tests cover all four January 2022 pairs: prior rejection, opt-in,
duplicate ambiguity, minimum date windows, indexed/full-scan equivalence,
caption removal, automatic replanning, cached-photo reuse and metadata export.
No real message mappings, backfill state or gallery metadata were modified
while collecting/verifying these fixtures.
