# January 2022 archive / Telegram 2374 recompression regression

Manually verified by the repository owner. Preserve original JPEG bytes.
Both images are 748 × 499 pixels.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2022/01/2022-01-12_F26ACB26.jpg>
  - 109,664 bytes; copied from the sibling archive and compared byte-for-byte
    with the public archive URL
  - SHA-256: `75eb9b0f5565b6c75d442d709205ec24153322d0477b66420c016100765710f3`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/2374>
  - Post date: `2022-03-04T18:24:34+00:00`, 51 days after the archive filename date
  - Largest still variant `x`, 127,417 bytes; no video variants
  - Selected size and existing disk-cache path verified through the Telegram API
  - SHA-256: `4e8651bb160bdddc18864820c79522aede63615c66ee915fe58aab3956012348`
  - Legacy digest: `a1f24e896225` (same as archive; exact bytes/pixels differ)

At 128px preview resolution, maximum RGB RMS is 4.260, luminance RMS is
2.020, and maximum chroma RMS is 1.812. The old luminance RMS cap of 2.0
rejects this pair. Luminance outliers (>8) are zero; maximum chroma outlier
fraction (>12) is 0.000061. Absolute signed mean shifts are Y 1.478,
Cb 1.016 and Cr 1.035.

The updated luminance RMS cap of 2.25 admits this slight recompression; all
outlier checks remain satisfied without the stronger-chroma edge branch.
Live verification found exactly one passing archive candidate among 1,991
within ±90 days of the Telegram post. Indexed matching gives the same result.
The minimum date window is ±51 days, not the default ±7 days.

Offline tests cover all four January 2022 pairs: prior rejection, opt-in,
duplicate ambiguity, minimum date windows, indexed/full-scan equivalence,
caption removal, automatic replanning, cached-photo reuse and metadata export.
No real message mappings, backfill state or gallery metadata were modified
while collecting/verifying these fixtures.
