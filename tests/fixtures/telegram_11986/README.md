# JPEG/chroma recompression visual-match regression

Manually verified archive/Telegram pair supplied by the repository owner. Both
images are 500 × 467 pixels. Preserve these original bytes; re-encoding would
change the comparison and invalidate the regression.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2024/04/2024-04-22_01-31-55.jpg>
  - 41,846 bytes
  - SHA-256: `302d74ff531b9bc7e198d1ee0b3fb7e02461f2efbbe8a8b64f729d155c4a90df`
- `telegram.jpg`: <https://t.me/RosaroterPanzerBackup/11986>
  - Message date: `2024-04-22T14:52:10+00:00`
  - Largest still-image variant (`x`), 53,712 bytes; no video downloaded
  - SHA-256: `dc5c656ec60940091535df6a941c9c722be2dda29abdd5442e20e6cec8cf573e`

On 128-pixel RGB previews, maximum channel RMS is approximately 2.843 and the
fraction of channel samples differing by more than 12 is approximately 0.001506.
The previous limits (RMS 2.5, outliers 0.001) incorrectly rejected this pair.
The new limits (RMS 3.0, outliers 0.002) admit modest recompression; both gates,
aspect-ratio checks, explicit visual opt-in and ambiguity rejection remain.

Live verification checked all 73 archive candidates within ±3 days of the post.
Exactly one passes the visual gates: the archive above. The closest other
aspect-compatible candidate has RMS approximately 90.675, far outside the limit.
Offline tests use this pair without a Telegram session or archive checkout.
