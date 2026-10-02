# Five-day archive lag and modest chroma-recompression regression

Manually verified pair supplied by the repository owner. Both images are
664 × 500 pixels. Preserve their original JPEG bytes.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2023/06/2023-06-13_31E2BD1B.jpg>
  - Filename date: `2023-06-13`
  - 88,056 bytes
  - SHA-256: `f8ad1c7dc4330df447c75dca20c9348fe8e17c3b2dc01287ca07395a51148d22`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/8347>
  - Post date: `2023-06-08T14:39:36+00:00`
  - Largest still-image variant (`x`), 127,651 bytes; no video downloaded
  - SHA-256: `6fba9b1c4e9ff6249c24efbd7379d2ef98dafda509fdd57170660978d6bd7ac8`

The archive date is five days after the post. The old ±3-day date window excludes
the correct candidate; the default ±7-day window includes it. Even within that
window, the prior chroma RMS cap of 2.5 rejected the pair:

- Maximum RGB RMS: approximately 4.645
- Luminance RMS: approximately 0.731; no luminance outliers (>8)
- Maximum chroma RMS: approximately 2.644
- Maximum chroma outlier fraction (>12): approximately 0.000244
- Maximum absolute signed YCbCr mean shift: approximately 0.029

Increasing only the chroma RMS cap to 3.0 accepts this modest recompression.
Other gates remain unchanged. The pair is the sole passing candidate among all
165 archive originals in the actual post's default date window. Tests run offline
and cover opt-in, ambiguity, date-window inclusion and rejection of larger chroma
changes even when luminance agrees.
