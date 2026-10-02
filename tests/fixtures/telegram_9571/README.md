# JPEG rounding-bias visual-match regression

Manually verified pair supplied by the repository owner. Both images are
606 × 462 pixels. Preserve their original JPEG bytes.

- `archive.jpg`: <https://archiv0.derrosarotepanzer.com/images/2023/09/2023-09-26_34ECB615.jpg>
  - 34,201 bytes
  - SHA-256: `69c971ae8a409296faa40316a97433571226bec12a9fafb5393715ffd3799b40`
- `telegram.jpg`: public photo CDN referenced by <https://t.me/RosaroterPanzerBackup/9571?embed=1>
  - Actual cached post date: `2023-09-25T20:40:13+00:00`
  - 43,638 bytes
  - SHA-256: `8e30509f7f9131c6e142cd7d69dd7f89e2d3b6fc396935e3a63f9d6ac85ea6c0`
  - The photo's legacy image digest (`05a4f56f4063`) agrees with the digest saved
    by the real backfill for message 9571. The public photo was downloaded without
    opening another Telegram client while the user's backfill was running.

On 128-pixel previews, maximum RGB RMS is approximately 3.631, luminance RMS
approximately 0.638, and maximum chroma RMS approximately 2.003. The prior
YCbCr fallback rejected only the mean-shift check: the maximum absolute signed
channel mean shift is approximately 0.818, over the old 0.5 cap.

Allowing at most 1.0 of the 255 channel levels admits modest JPEG rounding bias;
all RMS, outlier, aspect-ratio, visual opt-in and ambiguity gates remain unchanged.
Live archive-window verification considered 77 candidates within ±3 days of
September 25 and found exactly one match. No running backfill cache/state/metadata
was modified when collecting or verifying this fixture.
