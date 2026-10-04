import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops

from scripts import generate_thumbnails as thumbs
from scripts import migrate_transparent_sprites as migration


class TransparentSpriteMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.month = self.root / '2024/01'
        self.month.mkdir(parents=True)
        self.entries = [
            {'name': 'portrait.jpg', 'w': 1000, 'h': 2010, 'bg': 'ABC'},
            {'name': 'landscape.jpg', 'w': 2010, 'h': 1000},
            {'name': 'small.jpg', 'w': 10, 'h': 20},
            {'name': 'square.jpg', 'w': 500, 'h': 500},
            {'name': 'near-square.jpg', 'w': 960, 'h': 1000},
            {'name': 'thin.jpg', 'w': 1, 'h': 10000},
        ]
        self.index = self.month / 'entry_index.json'
        self.index.write_text(json.dumps(self.entries))
        self.path = self.month / 'thumbnails-00.webp'
        self.legacy_sheet(self.path, self.entries)

    def legacy_sheet(self, path, entries):
        sheet = Image.new('RGB', thumbs.sheet_dimensions(len(entries)), '#789')
        for i, entry in enumerate(entries):
            x, y = thumbs.tile_position(i)
            l, t, r, b = thumbs.tile_bounds(entry['w'], entry['h'])
            sheet.paste(('red', 'blue', 'white')[i % 3], (x+l, y+t, x+r, y+b))
        thumbs.save_sheet(sheet, path)

    def test_migration_preserves_vp8_payload_and_every_visible_rgb_pixel(self):
        before = self.path.read_bytes()
        index_before = self.index.read_bytes()
        with Image.open(self.path) as image:
            rgb_before = image.convert('RGB')
        self.assertTrue(migration.migrate_sheet(self.path, self.entries))
        original_chunks = dict(migration.riff_chunks(before))
        migrated_chunks = dict(migration.riff_chunks(self.path.read_bytes()))
        self.assertEqual(original_chunks[b'VP8 '], migrated_chunks[b'VP8 '])
        self.assertTrue(migrated_chunks[b'VP8X'][0] & 0x10)
        with Image.open(self.path) as image:
            self.assertEqual(image.mode, 'RGBA')
            mask = migration.image_mask(self.entries)
            self.assertIsNone(ImageChops.difference(image.getchannel('A'), mask).getbbox())
            difference = ImageChops.difference(rgb_before, image.convert('RGB'))
            self.assertIsNone(ImageChops.multiply(difference, mask.convert('RGB')).getbbox())
        self.assertEqual(self.index.read_bytes(), index_before)
        after = self.path.read_bytes()
        mtime = self.path.stat().st_mtime_ns
        self.assertFalse(migration.migrate_sheet(self.path, self.entries))
        self.assertEqual(self.path.read_bytes(), after)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    def test_geometry_includes_odd_rounding_small_images_gutters_and_empty_cells(self):
        mask = migration.image_mask(self.entries)
        self.assertEqual(mask.size, (1108, 442))
        for i, entry in enumerate(self.entries):
            x, y = thumbs.tile_position(i)
            l, t, r, b = thumbs.tile_bounds(entry['w'], entry['h'])
            self.assertEqual(mask.getpixel((x+l, y+t)), 255)
            self.assertEqual(mask.getpixel((x+r-1, y+b-1)), 255)
            if l > 0:
                self.assertEqual(mask.getpixel((x+l-1, y+t)), 0)
            if t > 0:
                self.assertEqual(mask.getpixel((x+l, y+t-1)), 0)
        self.assertEqual(mask.getpixel((220, 100)), 0, 'horizontal gutter')
        self.assertEqual(mask.getpixel((100, 220)), 0, 'vertical gutter')
        self.assertEqual(mask.getpixel((300, 300)), 0, 'unused final-row cell')

    def test_dry_run_and_quality_variants_use_only_local_files(self):
        variant = self.month / 'thumbnails-00-q50.webp'
        variant.write_bytes(self.path.read_bytes())
        before = {p.name: p.read_bytes() for p in self.month.iterdir()}
        with patch.object(thumbs, 'download', side_effect=AssertionError('no downloads')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(migration.migrate(self.root, [], workers=2, dry_run=True), 2)
            self.assertEqual({p.name: p.read_bytes() for p in self.month.iterdir()}, before)
            self.assertEqual(migration.migrate(self.root, ['2024/01'], workers=2), 2)
            self.assertEqual(migration.migrate(self.root, [], workers=2), 0)
        for path in [self.path, variant]:
            with Image.open(path) as image:
                self.assertEqual(image.getchannel('A').getpixel((220, 100)), 0)

    def test_existing_partial_alpha_inside_images_is_preserved(self):
        # Simulate genuine source transparency as well as opaque old padding.
        with Image.open(self.path) as original:
            rgba = original.convert('RGBA')
        x, y = 110, 110
        rgba.putpixel((x, y), (50, 100, 150, 128))
        thumbs.save_sheet(rgba, self.path)
        vp8_before = dict(migration.riff_chunks(self.path.read_bytes()))[b'VP8 ']
        self.assertTrue(migration.migrate_sheet(self.path, self.entries))
        with Image.open(self.path) as image:
            self.assertEqual(image.getchannel('A').getpixel((x, y)), 128)
            self.assertEqual(image.getchannel('A').getpixel((220, 100)), 0)
        self.assertEqual(dict(migration.riff_chunks(self.path.read_bytes()))[b'VP8 '], vp8_before)

    def test_metadata_chunks_are_preserved(self):
        with Image.open(self.path) as original:
            exif = Image.Exif()
            exif[305] = 'test encoder'
            original.save(self.path, 'WEBP', quality=55, exif=exif, xmp=b'<test/>')
        before = dict(migration.riff_chunks(self.path.read_bytes()))
        self.assertTrue(migration.migrate_sheet(self.path, self.entries))
        after = dict(migration.riff_chunks(self.path.read_bytes()))
        for kind in [b'VP8 ', b'EXIF', b'XMP ']:
            self.assertEqual(before[kind], after[kind])
        self.assertEqual(after[b'VP8X'][0], before[b'VP8X'][0] | 0x10)

    def test_geometry_mismatch_and_failed_verification_leave_sheet_untouched(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'dimensions/format'):
            migration.migrate_sheet(self.path, self.entries[:1])
        with patch.object(migration, 'with_alpha', return_value=before), self.assertRaises(ValueError):
            migration.migrate_sheet(self.path, self.entries)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.month.iterdir()), ['entry_index.json', 'thumbnails-00.webp'])

    def test_missing_sheet_preflight_does_not_modify_existing_sheets(self):
        entries = [{'name': f'{i}.jpg', 'w': 440, 'h': 220} for i in range(21)]
        self.index.write_text(json.dumps(entries))
        self.legacy_sheet(self.path, entries[:20])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'missing local sheet'):
            migration.migrate(self.root, [], workers=1)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'months without local indexes'):
            migration.migration_jobs(self.root, ['2023/01'])

    def test_riff_parser_handles_odd_chunks_and_rejects_truncation(self):
        encoded = migration.pack_webp([(b'TEST', b'123'), (b'DATA', b'12')])
        self.assertEqual(migration.riff_chunks(encoded), [(b'TEST', b'123'), (b'DATA', b'12')])
        bad_chunk = b'WEBP' + b'TEST' + (100).to_bytes(4, 'little') + b'1234'
        for data in [b'', encoded[:-1], b'RIFF' + len(bad_chunk).to_bytes(4, 'little') + bad_chunk]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                migration.riff_chunks(data)

    def test_opaque_lossless_webp_is_rejected_without_reencoding(self):
        Image.new('RGB', thumbs.sheet_dimensions(len(self.entries)), 'red').save(self.path, 'WEBP', lossless=True)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'static lossy VP8'):
            migration.migrate_sheet(self.path, self.entries)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
