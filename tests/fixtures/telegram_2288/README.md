# Already-matching regression: Telegram 2288

Manually verified by the repository owner. Preserve original JPEG bytes.
Both images are 613 × 500 pixels.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2022/01/2022-01-04_10D94086.jpg>
  - 88,825 bytes; copied from the sibling archive and verified byte-for-byte
    against the public URL
  - SHA-256: `932b340a5b1677c3f1271701d56410d6cc15d869b1a8b8b0f46d8d8b17bc628f`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/2288>
  - Post date: `2022-02-26T09:35:27+00:00` (53-day offset)
  - Largest still variant `x`, 85,735 bytes; no video variants
  - Telegram API verification confirmed selected variant and existing cache path
  - SHA-256: `5024c5fc79818e30fd81092f0abf816b0bd83432746cf4553d547419897d9c76`
  - Legacy digest: `4c074899199a` (same as archive; exact bytes/pixels differ)

This is not a visual-comparison false negative in version 9. It already passes
the unchanged ordinary RGB path, with maximum RMS 2.705 and outlier fraction
well below 0.2%. At collection time both the real message cache and gallery
metadata already linked this archive filename to message 2288.

Live verification found exactly one passing archive match among 1,991 candidates
within ±90 days of the actual post date. The coarse index gives the same result.
The minimum date window is ±53 days; the default ±7-day window excludes it.

Offline tests preserve the version-9 acceptance, visual opt-in, duplicate
ambiguity, date-window boundaries and caption-change rejection. An integration
test keeps this mapping and its existing counts unchanged while retries fill
the genuine gaps for 2025 and 2738. No real mappings, backfill state or gallery
metadata were modified during verification.
