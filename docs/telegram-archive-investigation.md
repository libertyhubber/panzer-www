# Investigation: 46 archive images without Telegram associations

## Matching status

The owner has reviewed the remaining matching issues and considers the
archive → Telegram association pass complete as far as it can go. The 18
unknown archive associations below are retained, not forced into speculative
matches. The next step is the reverse backfill: import Telegram photo attachments
that are absent from the archive, especially posts predating archiving.
See [`scripts/import_telegram.py`](../scripts/import_telegram.py) and the
[import workflow](../README.md#import-telegram-photos-missing-from-the-archive).

## Repair follow-up

The owner-approved repairs have now been applied in the website checkout and
`../panzer-archiv-00`:

- Repaired all **20 July mappings**, replacing nonexistent cached filenames with
  their verified archive counterparts. Also repaired four other stale mappings
  whose photos were verified reposts of indexed originals.
- Removed `images/2023/08/2023-08-12_5FE68E4E.jpg`, retained the identical July file,
  and redirected post 9062 to it. Refreshed archive and website indexes/counts.
- Replaced all **seven reviewed variants** with the downloaded Telegram JPEG bytes
  without recompression, and associated them with the reviewed posts.
- Regenerated affected archive JPEG thumbnails and website WebP sprites, including
  replacements whose filename and dimensions did not change.
- Automated classification refresh failed with HTTP 401 (invalid API key).
  Appended seven explicitly labelled manual caption corrections instead, updating
  the soap-image description/tags and exporting the classification/search indexes.
- Preserved existing engagement totals, and replanned the backfill cursor for the
  current remaining images.

**18 images remain unassociated:** the original 18 unknowns. In the subsequent
owner-requested duplicate cleanup, `2022-12-12_2D8A17A6.jpg` and
`2023-03-23_41E0161F.jpg` were removed. Posts 6358 and 7447 now point to the retained
`2022-12-16_2E27134E.jpg` and `2023-03-24_CCF9D1CB.jpg`, respectively. Archive and
website indexes, counts, thumbnails and metadata were refreshed. The retained
files and cached Telegram engagement totals were preserved.
One non-indexed cached mapping remains (unavailable post 17145), unchanged.

Audit details and SHA-256 hashes: [telegram-archive-repairs.json](telegram-archive-repairs.json).
A private pre-repair backup is at `/tmp/panzer-archive-repair-backup-b7zksu9s/`.
Nothing was committed or pushed.

The findings below describe the **pre-repair snapshot**.

## Findings

This is primarily a stale-mapping problem, not a visual-threshold problem.
The 46 images in `scripts/telegram_backfill_state.json` break down as follows:

| Count | Finding |
| ---: | --- |
| 20 | Live Telegram photos incorrectly skipped because cached mappings reference filenames absent from the archive indexes. All 20 pass the existing matcher. |
| 1 | Byte-identical duplicate of an archive image already linked to Telegram. |
| 5 | Related Telegram images with changed wording; correctly rejected by the strict matcher. |
| 2 | Related Telegram images with crop/layout/extra-caption differences; require explicit human review, not broader compression tolerance. |
| 18 | No corresponding image established by this investigation. Deletion, non-publication to this channel, or another variant remain possible. |

During the initial investigation, no mappings, gallery metadata, backfill cursors,
or archive originals were changed. Telegram metadata was fetched live, and selected
still photos were downloaded to its normal temporary disk cache for comparison.
The subsequent owner-approved changes are recorded in the repair follow-up above.

## 1. Twenty stale mappings hide straightforward July matches

`backfill_history()` in `scripts/backfill_telegram.py` skips a photo whenever
`record.get('name')` is truthy. `rank_history()` in
`scripts/review_telegram_matches.py` does the same. Neither verifies that the
cached filename exists in `matcher.image_dates`.

There are **25 named message-cache records whose names are absent from the
current archive indexes**. Twenty are exactly the posts needed for the unresolved
July batch. These stale filenames already occur in the HEAD version of
`telegram_messages_cache.json`; this is not a consequence of the recent visual
matching changes.

For example, message **13310** is cached as
`2024-07-30T164009_24a6047666dd.jpg`, which is not an indexed archive image.
Its actual photo matches **`2024-07-31_04-34-29.jpg`** with the current algorithm:
RGB RMS 1.291; review distance 0.672.

The archive dates and Telegram dates are swapped for these two batches:

- Telegram **2024-07-30** → archive **2024-07-31**.
- Telegram **2024-07-31** → archive **2024-07-30**.

Both offsets are only one day, within even the default ±7-day window. Widening the
window or using `--restart` alone cannot fix a post skipped as already mapped.

### Verified by direct photo comparison using the existing automatic matcher

All links below are in `https://t.me/RosaroterPanzerBackup/`.
RMS is the current matcher's maximum RGB-channel RMS on 128px previews.

| Archive filename | Telegram ID | Existing match result |
| --- | ---: | --- |
| `2024-07-31_04-34-29.jpg` | 13310 | visual, RMS 1.291 |
| `2024-07-31_04-34-26.jpg` | 13311 | visual, RMS 1.122 |
| `2024-07-31_04-34-24.jpg` | 13312 | visual, RMS 1.160 |
| `2024-07-31_04-34-22.jpg` | 13313 | visual, RMS 0.774 |
| `2024-07-31_04-34-19.jpg` | 13314 | visual, RMS 1.107 |
| `2024-07-31_04-34-17.jpg` | 13315 | visual, RMS 1.148 |
| `2024-07-31_04-34-15.jpg` | 13316 | visual, RMS 1.061 |
| `2024-07-31_04-34-12.jpg` | 13317 | visual, RMS 0.960 |
| `2024-07-31_04-34-07.jpg` | 13318 | visual, RMS 1.445 |
| `2024-07-31_04-34-01.jpg` | 13319 | visual, RMS 0.923 |
| `2024-07-30_8ys0kk.jpg` | 13323 | visual, RMS 1.123 |
| `2024-07-30_8ysoux.jpg` | 13324 | visual, RMS 1.559 |
| `2024-07-30_8ysqth.jpg` | 13325 | visual, RMS 0.910 |
| `2024-07-30_8ysqti.jpg` | 13326 | identical bytes |
| `2024-07-30_8ysrmo.jpg` | 13327 | visual, RMS 1.016 |
| `2024-07-30_8yss47.jpg` | 13328 | visual, RMS 1.504 |
| `2024-07-30_8yss48.jpg` | 13329 | identical bytes |
| `2024-07-30_8yt3mg.jpg` | 13330 | visual, RMS 1.290 |
| `2024-07-30_8yt4wv.jpg` | 13331 | visual, RMS 1.780 |
| `2024-07-30_8yt5x4.jpg` | 13332 | visual, RMS 2.567 |

The review distances for the true matches are **0–1.084**, not the 43–80+ scores
shown for unrelated suggestions. The algorithm could recognize them; it never
received those candidates.

The other five non-indexed mappings are posts 13565, 14642, 14725, 15030 and
17145. Live photos for the first four match already-indexed originals byte for
byte (reposts); post 17145 was unavailable. These five do not fill any of the 46
archive gaps. In particular, invalidating every stale mapping must not blindly
create new archive links or discard cached statistics.

## 2. One true archive duplicate

`2023-07-28_5B8DAC3F.jpg` and `2023-08-12_5FE68E4E.jpg` are byte-identical:

```text
SHA-256: 0bfc7456ea56a985a4dd1abd7e6b56ee30e65d197319247015558ae9816044aa
```

The second filename is already linked to
[Telegram 9062](https://t.me/RosaroterPanzerBackup/9062), dated 2023-08-09.
The live Telegram photo passes the visual comparison against either original.
The full automatic matcher returns **ambiguous**, correctly, because both
archive filenames qualify. The review script excludes post 9062 as already mapped.

This requires a duplicate/alias policy, not relaxed matching. A message-cache
record has only one `name`; linking both files to one post requires either gallery
aliases or consolidating the duplicate archive entries. The existing link should
not simply be moved from one filename to the other, which would trade one gap for
another.

## 3. Five changed-text variants

These pairs were inspected visually. They are not equivalent original images.

| Pending archive image | Related Telegram post | Difference |
| --- | --- | --- |
| `2022-05-10_E9C63CAF.jpg` | [3681](https://t.me/RosaroterPanzerBackup/3681) | Same soapy-finger photo; archive discusses an Instagram restriction, Telegram has a different handwashing caption. |
| `2022-12-12_2D8A17A6.jpg` | [6358](https://t.me/RosaroterPanzerBackup/6358) | First speech bubble changes from “Ich werde angegriffen!” to “Er greift mich an!”. Post is already linked to `2022-12-16_2E27134E.jpg`. |
| `2023-03-23_41E0161F.jpg` | [7447](https://t.me/RosaroterPanzerBackup/7447) | Elmo caption changes “den gegen den Stolperdraht” to “gegen den Stolperdraht”, with reflow/resizing. Post is already linked to `2023-03-24_CCF9D1CB.jpg`. |
| `2023-04-27_52957A5A.jpg` | [7830](https://t.me/RosaroterPanzerBackup/7830) | “keine Ungeimpfte daten” changes to “KEINE GEIMPFTE DATEN”, reversing the meaning. |
| `2024-03-28_0DE231DA.jpg` | [11703](https://t.me/RosaroterPanzerBackup/11703) | “Wussen Sie schon?” corrected to “Wussten Sie schon?”, with different caption rendering/layout. |

Do not lower the global visual thresholds to accept these. The first-speech-bubble
example has a review score of only about 3.27 against Telegram despite genuinely
changed text: a low pixel distance alone does not establish identity.

## 4. Two crop/layout variants requiring owner review

| Pending archive image | Related Telegram post | Difference |
| --- | --- | --- |
| `2022-10-01_D2523E92.jpg` | [5671](https://t.me/RosaroterPanzerBackup/5671) | Same artwork and wording, but artwork/caption proportions and layout differ. Review score 52.647. |
| `2023-06-14_C382CE15.jpg` | [8367](https://t.me/RosaroterPanzerBackup/8367) | Same “Disneys 101 Dalmatiner” dog image; archive contains an additional partially clipped lower caption, absent from Telegram, with differing crop/layout. Review score 44.514. |

The current comparator is intentionally not crop/layout invariant. If these are
acceptable associations under the owner's policy, use explicit, byte-pinned manual
approvals rather than broadening automatic identity matching. The extra caption
in the dog image is a content difference, not just JPEG recompression.

## 5. Eighteen cases remain unresolved

```text
2022-04-10_21904E3B.jpg
2022-04-30_5266D76B.jpg
2022-04-30_A904ACC2.jpg
2022-04-30_DD2E18E5.jpg
2022-05-01_0BC9B602.jpg
2022-05-04_04383F18.jpg
2022-05-06_653B1DFD.jpg
2022-05-06_71990853.jpg
2022-05-11_0AD6A371.jpg
2022-06-03_254F5D7C.jpg
2022-06-09_7F686039.jpg
2022-06-14_4E3FE67B.jpg
2022-08-09_AE47F360.jpg
2022-10-08_A9DF271A.jpg
2023-05-03_1D84687D.jpg
2023-06-13_1167BAFF.jpg
2023-10-08_65A262B4.jpg
2024-01-02_72305058.jpg
```

Some archive images are visibly templates or local edits: the empty speech bubble
in `2022-04-30_DD2E18E5.jpg`, the filled version on May 1, and the annotated and
unannotated bingo images. These are related, but not duplicate originals.
The closest suggestions inspected for this group were unrelated images, not
missed JPEG matches.

It is not possible to conclude “deleted” from the provided ±90-day unmatched-only
scan. For example, live channel IDs are contiguous around 2023-10-08 and
2024-01-02; there is no same-day missing ID proving deletion of those images.
Conversely, archive filename dates can differ from publication dates, so those
checks also do not disprove deletion elsewhere. Images could have been published
only on another platform or retained locally without publication.

## Checks performed and limits

- Loaded the current archive indexes: **19,243 indexed originals** across 2021–2026.
- Decoded all originals and compared SHA-256 byte and decoded-pixel identities
  against the 46 pending images, without date restrictions.
- Checked the current strict visual comparator against the full archive using its
  conservative coarse shortlist. The July-28/August-12 duplicate was the only
  other archive original passing for a pending image.
- Used coarse nearest-neighbor rankings and existing classification text as
  investigative leads; those rankings are not an exhaustive crop-invariant search.
- Fetched live message/media metadata around July 25–August 4, January 1–4, and
  October 7–10, and directly fetched selected posts/photos for the comparisons above.
- Tested the 20 July photos with the actual `ArchiveMatcher.match()` against all
  indexed candidates in ±90 days; all yielded unique accepted matches.
- Inspected selected top-ranked unresolved suggestions and related mapped posts.
- Did **not** rerun a complete all-history Telegram-photo scan including every
  mapped post. Thus the 18 unresolved cases are not proven absent from all history.

## Recommended next steps

1. Treat a cached name as “already mapped” only when it is present in the archive
   indexes. Preserve its previous value for diagnosis and preserve engagement fields.
   Apply this check to both backfill and review; also invalidate/restart an exhausted
   history plan when stale mappings become eligible.
2. Repair/re-match the 20 July records using the existing thresholds. This would
   reduce the original 46 gaps to **26**, without any algorithm relaxation.
3. Add a review option to include genuinely mapped posts, explicitly showing their
   linked filename. This is needed for duplicate and alternate-version diagnosis.
4. Decide the archive-duplicate/alias policy for the July-28/August-12 pair.
5. Keep changed-text variants distinct. Consider explicit owner approvals for
   acceptable rendering/crop variants, with byte-pinned evidence as already supported.
6. Investigate the 18 unknown cases with broader history including mapped posts,
   or source/export records. Do not label them deleted solely because no candidate
   passed the current search.
