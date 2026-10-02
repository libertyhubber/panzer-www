# Archive-date lag regression

Manually verified pair supplied by the repository owner. Both images are
500 × 669 pixels. Preserve their original JPEG bytes.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2023/06/2023-06-27_4B728817.jpg>
  - Archive filename date: `2023-06-27`
  - 114,262 bytes
  - SHA-256: `6ec29ebe387da1b6ef443818732824340d29677dd162786cc63ae24b3d252e30`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/8513>
  - Post date: `2023-06-23T20:13:05+00:00`
  - Largest still-image variant (`x`), 112,044 bytes; no video downloaded
  - SHA-256: `16bec3cc7a7ed30aadf466609d09c2adc0c65ce32af57ba390d5204feecaba9f`

The archive filename is four calendar days later than the Telegram post. The
existing visual comparator accepts the pair with RGB RMS approximately 2.363;
it was excluded from the old ±3-day candidate/search window, not rejected by
visual thresholds. Exact bytes and decoded pixels differ.

Live archive checks at the actual post date:

- ±3 days: 77 candidates; the correct image is excluded.
- ±4 days: 99 candidates; the correct image is the unique visual match.
- ±7 days (new default): 165 candidates; the correct image remains unique.

Offline regressions check direct comparison, candidate selection and gap-window
planning. Date-window changes already invalidate saved plan signatures, so an
old cursor cannot prevent retrying previously excluded images.
