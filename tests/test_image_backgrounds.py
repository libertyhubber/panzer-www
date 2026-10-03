import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from scripts import generate_thumbnails as thumbs
from scripts import ingest_uploads as ingest


class BackgroundColorTests(unittest.TestCase):
    def test_counts_outer_band_not_interior(self):
        image = Image.new('RGB', (100, 100), 'white')
        image.paste('red', (5, 5, 95, 95))
        self.assertEqual(thumbs.background_color(image), 'FFF')

    def test_thin_black_frame_does_not_override_white_background(self):
        image = Image.new('RGB', (500, 743), 'black')
        image.paste('white', (3, 3, 497, 740))
        image.paste('gray', (50, 100, 350, 700))
        # Exercise JPEG noise as well as the frame.
        source = io.BytesIO()
        image.save(source, 'JPEG')
        source.seek(0)
        with Image.open(source) as original:
            self.assertEqual(thumbs.background_color(original), 'FFF')

    def test_counts_all_four_sides(self):
        image = Image.new('RGB', (4, 20), 'red')
        image.paste('blue', (0, 1, 1, 19))
        image.paste('blue', (3, 1, 4, 19))
        self.assertEqual(thumbs.background_color(image), '00F')

    def test_corners_count_once(self):
        image = Image.new('RGB', (3, 3), 'white')
        for point in [(0, 0), (2, 0), (0, 2)]:
            image.putpixel(point, (0, 0, 0))
        # Three black corners versus five white edge pixels.
        self.assertEqual(thumbs.background_color(image), 'FFF')

    def test_nearby_colors_vote_together_after_rounding(self):
        image = Image.new('RGB', (5, 1), (0, 0, 0))
        for x, color in enumerate([(254, 253, 255), (252, 255, 254), (255, 255, 255)]):
            image.putpixel((x, 0), color)
        self.assertEqual(thumbs.background_color(image), 'FFF')
        self.assertEqual(thumbs.background_color(Image.new('RGB', (1, 1), (8, 9, 128))), '018')

    def test_ties_and_degenerate_dimensions(self):
        for size in [(1, 1), (1, 5), (5, 1), (2, 2)]:
            with self.subTest(size=size):
                self.assertEqual(thumbs.background_color(Image.new('RGB', size, 'black')), '000')
        image = Image.new('RGB', (1, 2), 'white')
        image.putpixel((0, 0), (0, 0, 0))
        self.assertEqual(thumbs.background_color(image), '000')

    def test_grayscale_and_palette_images(self):
        self.assertEqual(thumbs.background_color(Image.new('L', (10, 10), 255)), 'FFF')
        palette = Image.new('P', (10, 10), 0)
        palette.putpalette([255, 0, 255] + [0] * 765)
        self.assertEqual(thumbs.background_color(palette), 'F0F')

    def test_tile_padding_uses_bg(self):
        source = io.BytesIO()
        Image.new('RGB', (440, 220), 'red').save(source, 'PNG')
        source.seek(0)
        tile = thumbs.make_tile(source, 'ABC')
        self.assertEqual(tile.getpixel((110, 54)), (170, 187, 204))
        self.assertEqual(tile.getpixel((110, 55)), (255, 0, 0))

    def test_background_validation_and_legacy_fallback(self):
        entry = {'name': 'image.jpg', 'w': 440, 'h': 220}
        self.assertEqual(thumbs.gallery_entries([entry]), [entry])
        self.assertEqual(thumbs.gallery_entries([{**entry, 'bg': 'abc'}]), [{**entry, 'bg': 'ABC'}])
        for bg in ['#FFF', 'FFFFFF', '', 'GGG', None, 123]:
            with self.subTest(bg=bg), self.assertRaisesRegex(ValueError, 'invalid background color'):
                thumbs.gallery_entries([{**entry, 'bg': bg}])

    def test_nearly_square_threshold_is_strict_and_orientation_independent(self):
        for width, height, expected in [
            (100, 100, False), (99, 100, False), (98, 100, False),
            (951, 1000, False), (95, 100, True), (949, 1000, True), (50, 100, True),
        ]:
            for size in [(width, height), (height, width)]:
                with self.subTest(size=size):
                    self.assertEqual(ingest.needs_background(*size), expected)

    def test_ingest_omits_nearly_square_bg_without_sampling_and_caches_dimensions(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            archive = Path(temporary)
            month = archive / 'images/2024/01'
            month.mkdir(parents=True)
            sizes = [(100, 100), (96, 100), (100, 96)]
            for index, size in enumerate(sizes):
                Image.new('RGB', size, 'white').save(month / f'{index}.jpg', 'JPEG')
            with patch.object(ingest, 'background_color', side_effect=AssertionError('square sampled')):
                ingest.update_indexes(archive)
            index_path = month / 'entry_index.json'
            entries = json.loads(index_path.read_bytes())
            self.assertEqual(entries, [
                {'name': f'{index}.jpg', 'w': size[0], 'h': size[1]}
                for index, size in enumerate(sizes)
            ])
            # Remove an obsolete bg using cached dimensions, without opening originals.
            entries[0]['bg'] = 'FFF'
            index_path.write_text(json.dumps(entries))
            with patch.object(ingest.Image, 'open', side_effect=AssertionError('square reopened')):
                ingest.update_indexes(archive, refresh_backgrounds=True)
                before = index_path.read_bytes()
                ingest.update_indexes(archive)
            self.assertEqual(index_path.read_bytes(), before)
            self.assertTrue(all('bg' not in entry for entry in json.loads(before)))
            # The exact 5% boundary still receives an edge color in both orientations.
            for index, size in enumerate([(95, 100), (100, 95)], start=3):
                Image.new('RGB', size, 'white').save(month / f'{index}.jpg', 'JPEG')
            ingest.update_indexes(archive)
            entries = json.loads(index_path.read_bytes())
            self.assertEqual([entry['bg'] for entry in entries[3:]], ['FFF', 'FFF'])

    def test_ingest_backfills_and_caches_bg_and_updates_sprites(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            archive = root / 'archive'
            month = archive / 'images/2024/01'
            month.mkdir(parents=True)
            source = month / 'original.jpg'
            Image.new('RGB', (440, 220), 'white').save(source, 'JPEG')
            index_path = month / 'entry_index.json'
            index_path.write_text(json.dumps([{'name': source.name, 'w': 440, 'h': 220}]))
            # Simulate an existing legacy black sprite/index.
            www = root / 'www'
            local = www / 'images/2024/01'
            thumbs.update_thumbnails(archive, www)
            ingest.update_indexes(archive)
            entry = json.loads(index_path.read_bytes())[0]
            self.assertEqual(entry, {'name': 'original.jpg', 'w': 440, 'h': 220, 'bg': 'FFF'})
            before = index_path.read_bytes()
            with patch.object(ingest.Image, 'open', side_effect=AssertionError('cached original reopened')):
                ingest.update_indexes(archive)
            self.assertEqual(index_path.read_bytes(), before)
            self.assertEqual(thumbs.update_thumbnails(archive, www), 1)
            self.assertEqual(json.loads((local / 'entry_index.json').read_bytes())[0]['bg'], 'FFF')
            with Image.open(local / 'thumbnails-00.webp') as sheet:
                self.assertTrue(all(channel > 245 for channel in sheet.getpixel((110, 10))))
            self.assertEqual(thumbs.update_thumbnails(archive, www), 0)
            # A refresh recalculates old cached colors without touching classifications.
            entry['bg'] = '000'
            index_path.write_text(json.dumps([entry]))
            ingest.update_indexes(archive)
            self.assertEqual(json.loads(index_path.read_bytes())[0]['bg'], '000')
            with patch.object(ingest, 'update_classifications', side_effect=AssertionError('classification called')):
                ingest.update_indexes(archive, refresh_backgrounds=True)
            self.assertEqual(json.loads(index_path.read_bytes())[0]['bg'], 'FFF')
            # Newly ingested originals receive bg too, with no legacy entry.
            Image.new('RGB', (10, 20), 'black').save(month / 'new.jpg', 'JPEG')
            ingest.update_indexes(archive)
            self.assertEqual(json.loads(index_path.read_bytes())[0]['bg'], '000')


if __name__ == '__main__':
    unittest.main()
