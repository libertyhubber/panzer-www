# Highest-resolution photo selection regression

Manually verified pair supplied by the repository owner. The original archive
image and Telegram's full-resolution still are both 500 × 812 pixels. Preserve
the original JPEG bytes, including the smaller thumbnail that exposed the bug.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2023/07/2023-07-24_B6866461.jpg>
  - 56,797 bytes
  - SHA-256: `eec3a9b98372dba9cf8696f1c1209ec951acf174630a52fa28caaa0e8de79a81`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/8852>, still variant `y`
  - Post date: `2023-07-23T17:24:32+00:00`
  - Full-resolution 500 × 812, 73,910 bytes
  - SHA-256: `44906bded03c2a4ad61f7434957c9b528d79fd3a877be4c25f9e9c50a1c0e951`
- `thumbnail.jpg`: same post, still variant `x`
  - Reduced-resolution 493 × 800, 74,149 bytes
  - SHA-256: `9d92040471270113eb64f8cbff0654bfef5c830468582307d4c199a75c6039d4`

The reduced thumbnail has more JPEG bytes than the full-resolution variant.
The former byte-count-based selection chose `x`, which the conservative visual
comparator rejects after Telegram's downsampling (RGB RMS approximately 3.936,
luminance RMS approximately 3.674). The correct `y` variant passes the existing
RGB gates with RMS approximately 1.696. No visual threshold changes are needed.

Select still-image variants by pixel area first, with byte count only as a
same-resolution tie-breaker. Video variants remain excluded. Live verification
finds exactly one match among all 165 archive candidates in the default window.
Both Telegram variants were fetched through Telethon after the user's running
backfill finished; the public embed uses the identical `x` bytes.
