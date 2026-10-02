# JPEG chroma-edge visual-match regression

Manually verified pair supplied by the repository owner. Both images are
591 × 413 pixels. Preserve their original JPEG bytes.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2024/03/2024-03-07_C3275E5B.jpg>
  - 31,784 bytes
  - SHA-256: `fc3dbadc968871ae133b8f1d41b1ea4dc526edd931e258e37ea73aa2dfb3eab9`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/11444>
  - Post date: `2024-03-05T17:58:07+00:00` (two days before the archive filename)
  - Largest still-image variant (`x`), 42,996 bytes; no video downloaded
  - SHA-256: `6f8fe6b1591248532875138aa9ddb7f874dc98d248ee5403bd9c305e9886bc4c`

The former RGB-only gates rejected the pair: maximum channel RMS approximately
4.151 and combined outlier fraction approximately 0.008687. The differences are
concentrated in JPEG color edges, especially the blue channel.

YCbCr comparisons of the same 128-pixel previews give:

- Luminance RMS approximately 0.859; no luminance samples differ by more than 8.
- Maximum chroma RMS approximately 2.225.
- Maximum per-chroma-channel outlier fraction approximately 0.009399 (>12).
- Maximum absolute signed channel mean shift approximately 0.270.

The bounded JPEG/chroma fallback accepts this pair while retaining explicit
visual opt-in, aspect-ratio checking, luminance/content and color-shift gates,
and ambiguity rejection. Live verification covers all 77 archive candidates
within ±3 days of the actual post; only this archive image passes the aspect
check and visual gates. Offline tests also check the two-day date offset.
