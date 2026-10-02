# Real Telegram/archive visual-match regression

This is a manually verified pair provided by the repository owner. Both images
are 661 × 355 pixels, but Telegram's JPEG bytes and decoded pixels differ from
the archive original. Keep the original bytes: re-encoding would change the test.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2024/08/2024-08-10T140659_f134bf89152ff368zu2l2.jpg>
  - 87,689 bytes
  - SHA-256: `f134bf89152ff36d488f7d8b0c50cbb3788f2c3f5dc5631d89080cb7571ca0e7`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/13457>
  - Message date: `2024-08-10T19:34:11+00:00`
  - Largest still-image variant (`x`), 56,543 bytes; no video downloaded
  - SHA-256: `7ae8cbd8c689b32d802c7999a91a76e09b1b23a91ab52087568d6d21238fa1d6`

The existing 128-pixel RGB comparison gives maximum channel RMS approximately
0.984 and an outlier fraction of 0 for this pair. A live verification against all
75 archive candidates within ±3 days found exactly one passing visual candidate,
the archive filename above. Tests run offline from these fixtures and cover opt-in,
ambiguity rejection and archive-index/date-window integration; they don't require
a Telegram session or sibling archive checkout. No backfill state/cache was changed
when collecting the fixtures.
