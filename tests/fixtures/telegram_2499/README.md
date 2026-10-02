# January 2022 archive / Telegram 2499 recompression regression

Manually verified by the repository owner. Preserve original JPEG bytes.
Both images are 500 × 875 pixels.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2022/01/2022-01-23_E612B5E2.jpg>
  - 117,883 bytes; copied from the sibling archive and compared byte-for-byte
    with the public archive URL
  - SHA-256: `cddf7bf88bd67df6fd5f6c3400630fe842cd17a50e5b6369fbbce1eabe14cd7c`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/2499>
  - Post date: `2022-03-12T18:09:19+00:00`, 48 days after the archive filename date
  - Largest still variant `y`, 144,404 bytes; smaller `x` is 457 × 800 pixels
  - No video variants; selected size and existing disk-cache path verified
    through the Telegram API
  - SHA-256: `e42b29c8cde3a6de0167fd686a9796edb93d617f19696b38930c93a3b8cc26e0`
  - Legacy digest: `0912909bc773` (same as archive; exact bytes/pixels differ)

At 128px preview resolution, maximum RGB RMS is 6.440, luminance RMS is
2.076 (old cap: 2.0), and maximum chroma RMS is 3.258 (old cap: 3.25).
Luminance outlier fraction (>8) is 0.000183; maximum chroma outlier fraction
(>12) is 0.002747. Absolute signed mean shifts are Y 1.519 (old cap: 1.5),
Cb 0.974 and Cr 1.454 (old chroma mean cap: 1.25).

The updated RMS/mean ceilings admit this small additional recompression bias.
Because raw chroma RMS exceeds 3.25, the guarded edge check also applies:
1px Gaussian chroma blur reduces maximum RMS to 1.745 and outliers to zero;
raw outliers outside shared color structure remain below 0.0001. Luminance
is never blurred and all original luminance outlier checks remain in force.

Live verification found exactly one passing archive candidate among 1,991
within ±90 days of the Telegram post. Indexed matching gives the same result.
The minimum date window is ±48 days, not the default ±7 days.

Offline tests cover all four January 2022 pairs: prior rejection, opt-in,
duplicate ambiguity, minimum date windows, indexed/full-scan equivalence,
caption removal, automatic replanning, cached-photo reuse and metadata export.
No real message mappings, backfill state or gallery metadata were modified
while collecting/verifying these fixtures.
