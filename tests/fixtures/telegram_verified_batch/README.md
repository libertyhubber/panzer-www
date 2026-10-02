# Owner-verified 43-pair regression batch

These archive/Telegram associations were supplied and manually verified by the
repository owner. Each numeric directory contains the original `archive.jpg`
and largest selected still-image `telegram.jpg`, without re-encoding.
`manifest.json` records filenames, actual UTC post dates, selected variants,
dimensions, SHA-256 hashes and whether an explicit manual confirmation is needed.

Archive URLs are `https://archiv0.derrosarotepanzer.com/images/YYYY/MM/NAME`;
Telegram URLs are `https://t.me/RosaroterPanzerBackup/MESSAGE_ID`. Originals were
copied from the sibling archive and **all 43 were verified byte-for-byte against
the public archive URL**. Telegram API retrieval verified the current message,
selected full-size still variant and existing photo-cache identity. Original
bytes were copied from that cache; access hashes/file references are not stored.

## General visual threshold changes

32 pairs pass the updated general JPEG/color-edge comparator. Compared with
version 10, the largest relevant cases require more allowance for dense chroma
artifacts around text, slight JPEG channel bias and luminance rounding outliers:

- Luminance RMS up to 3.0; outlier fraction (>8) up to 0.00225 (36 pixels at 128px).
- Raw chroma RMS up to 6.5, with the ordinary 3.25 RMS / 1% outlier path unchanged.
- Stronger chroma branch: raw outliers up to 8%, blurred RMS up to 4.5 and blurred
  outliers up to 4%, but flat-region raw outliers no more than 0.8%.
- Beyond the old blurred 3.25 RMS / 1% outlier limits, at least 1% of all pixels
  must have raw chroma outliers on structure shared by both originals.
- Absolute mean biases up to 2.0 (Y) / 1.875 (Cb/Cr).

Luminance is never blurred. Changed captions, large flat-region recoloring,
broad color casts and balanced color changes without shared edge evidence remain
negative regression cases. RGB gates, aspect-ratio checks, opt-in and duplicate
ambiguity rejection are unchanged.

Live verification scanned **all nearby archive candidates, including already
linked originals**, within ±90 days of each actual post date. Each of these 32
pairs is the unique passing general match; full scans and coarse-index results
agree. There were 1,728–2,023 candidates per window across the batch and 11,796
unique archive originals indexed in total.

## Explicit confirmations, not generic perceptual matches

The other 11 pairs are intentionally not admitted by the general comparator:

| Telegram ID | Archive filename | Difference outside JPEG bounds |
|---:|---|---|
| 13380 | `2024-08-04T153659_986f307582687d88z8emk.jpg` | Caption/photo spacing and height differ |
| 13120 | `2024-07-14T155314_bb6da70c4872ed08wyzp3.jpg` | Caption/photo spacing and height differ |
| 13023 | `2024-07-07T174815_9028395ccad91a88w6xgv.jpg` | Caption/photo spacing and height differ |
| 9167 | `2023-08-21_2A5073E1.jpg` | Larger resizing/color differences |
| 8759 | `2023-07-17_773DB87D.jpg` | Large resize and luminance-edge differences |
| 8093 | `2023-05-21_51F3AF86.jpg` | Broad color differences beyond mean limits |
| 6924 | `2023-02-04_F79A8520.jpg` | Larger resizing/color differences |
| 4789 | `2022-08-06_46121A21.jpg` | Luminance/layout differences beyond bounds |
| 4332 | `2022-07-03_8FA5596F.jpg` | Same wording, resized/repositioned captions |
| 3941 | `2022-06-01_FD0375A4.jpg` | Different sizes/rasterization, larger luminance differences |
| 3782 | `2022-05-17_D731DABC.jpg` | Same wording, resized/repositioned captions |

The owner's explicit confirmations are pinned in
`scripts/telegram_verified_matches.json` to channel, message ID, calendar post
date and both image SHA-256 hashes. This is a deliberate approval of these exact
pairs, not training a matcher to ignore arbitrary caption changes. They still
require visual opt-in and date-window eligibility, and byte/pixel identities
retain precedence. Reposted, edited or re-encoded media cannot reuse an approval
unless all its pins match. All 11 confirmations were checked against the full
live date windows and yielded the requested archive association.

## Offline coverage

Tests verify all 43 associations, unique ordinary matches, duplicate ambiguity,
manual opt-in, pinning of message IDs/dates/channel/bytes, stale or missing
approvals, exact-match precedence, date-window eligibility, changed-content
rejection and automatic replanning of an exhausted search. An end-to-end fake
history scan exports all 43 associations and reuses each downloaded photo on
retry, while preserving existing mappings/counts. No real message mappings,
gallery metadata or backfill state were modified while collecting/verifying
these fixtures.
