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
    def test_tile_padding_is_transparent_with_opaque_image_pixels(self):
        source = io.BytesIO()
        Image.new('RGB', (440, 220), 'red').save(source, 'PNG')
        source.seek(0)
        tile = thumbs.make_tile(source)
        self.assertEqual(tile.getpixel((110, 54)), (0, 0, 0, 0))
        self.assertEqual(tile.getpixel((110, 55)), (255, 0, 0, 255))

    def test_background_validation_and_legacy_fallback(self):
        entry = {'name': 'image.jpg', 'w': 440, 'h': 220}
        self.assertEqual(thumbs.gallery_entries([entry]), [entry])
        self.assertEqual(thumbs.gallery_entries([{**entry, 'bg': 'abc'}]), [{**entry, 'bg': 'ABC'}])
        for bg in ['#FFF', 'FFFFFF', '', 'GGG', None, 123]:
            with self.subTest(bg=bg), self.assertRaisesRegex(ValueError, 'invalid background color'):
                thumbs.gallery_entries([{**entry, 'bg': bg}])

    def test_obsolete_edge_fields_are_discarded_for_every_aspect_ratio(self):
        for width, height in [(100, 100), (100, 200), (200, 100)]:
            entry = {'name': 'image.jpg', 'w': width, 'h': height, 'bg': 'ABC'}
            for g in ['FFF000', '#FFF000', 'FFF', '', None, 123]:
                with self.subTest(size=(width, height), g=g):
                    self.assertEqual(thumbs.gallery_entries([{**entry, 'g': g}]), [entry])

    def test_ingest_averages_all_aspect_ratios_and_removes_old_g_without_opening_images(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            archive = Path(temporary)
            month = archive / 'images/2024/01'
            month.mkdir(parents=True)
            sizes = [(100, 100), (96, 100), (100, 96), (95, 100), (100, 95), (50, 100), (100, 50)]
            for index, size in enumerate(sizes):
                Image.new('RGB', size, 'white').save(month / f'{index}.jpg', 'JPEG')
            ingest.update_indexes(archive)
            index_path = month / 'entry_index.json'
            expected = [{'name': f'{i}.jpg', 'w': size[0], 'h': size[1], 'bg': 'FFF'}
                        for i, size in enumerate(sizes)]
            self.assertEqual(json.loads(index_path.read_bytes()), expected)
            # No recomputation is needed just to drop obsolete cached edge colors.
            index_path.write_text(json.dumps([{**entry, 'g': 'FFF000'} for entry in expected]))
            with patch.object(ingest.Image, 'open', side_effect=AssertionError('cached original reopened')):
                ingest.update_indexes(archive)
                before = index_path.read_bytes()
                ingest.update_indexes(archive)
            self.assertEqual(json.loads(before), expected)
            self.assertEqual(index_path.read_bytes(), before)

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
            sprite_before = (local / 'thumbnails-00.webp').read_bytes()
            self.assertEqual(thumbs.update_thumbnails(archive, www), 0)
            self.assertEqual((local / 'thumbnails-00.webp').read_bytes(), sprite_before)
            self.assertEqual(json.loads((local / 'entry_index.json').read_bytes())[0]['bg'], 'FFF')
            with Image.open(local / 'thumbnails-00.webp') as sheet:
                self.assertEqual(sheet.getpixel((110, 10))[3], 0)
                self.assertTrue(all(channel > 245 for channel in sheet.getpixel((110, 110))[:3]))
            entry['bg'] = '000'
            index_path.write_text(json.dumps([entry]))
            ingest.update_indexes(archive)
            self.assertEqual(json.loads(index_path.read_bytes())[0]['bg'], '000')
            with patch.object(ingest, 'update_classifications', side_effect=AssertionError('classification called')):
                ingest.update_indexes(archive, refresh_backgrounds=True)
            self.assertEqual(json.loads(index_path.read_bytes())[0]['bg'], 'FFF')
            Image.new('RGB', (10, 20), 'black').save(month / 'new.jpg', 'JPEG')
            ingest.update_indexes(archive)
            self.assertEqual(json.loads(index_path.read_bytes())[0]['bg'], '000')

    def test_refresh_replaces_cached_bg_without_rebuilding_sprites(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            archive = root / 'archive'
            month = archive / 'images/2024/01'
            month.mkdir(parents=True)
            image = Image.new('RGB', (100, 200), 'black')
            image.paste('white', (0, 0, 100, 80))
            image.save(month / 'image.jpg', 'PNG')
            index = month / 'entry_index.json'
            index.write_text(json.dumps([{'name': 'image.jpg', 'w': 100, 'h': 200,
                                          'bg': '000', 'g': '000000'}]))
            www = root / 'www'
            thumbs.update_thumbnails(archive, www)
            sprite = www / 'images/2024/01/thumbnails-00.webp'
            before = sprite.read_bytes()
            with patch.object(ingest.Image, 'open', side_effect=AssertionError('cached original reopened')):
                ingest.update_indexes(archive)
            self.assertEqual(json.loads(index.read_bytes())[0],
                             {'name': 'image.jpg', 'w': 100, 'h': 200, 'bg': '000'})
            ingest.update_indexes(archive, refresh_backgrounds=True)
            self.assertEqual(json.loads(index.read_bytes())[0],
                             {'name': 'image.jpg', 'w': 100, 'h': 200, 'bg': '666'})
            self.assertEqual(thumbs.update_thumbnails(archive, www), 0)
            self.assertEqual(sprite.read_bytes(), before)
            self.assertEqual(json.loads((www / 'images/2024/01/entry_index.json').read_bytes())[0]['bg'], '666')

    def test_exif_orientation_sets_display_dimensions_without_edge_fields(self):
        image = Image.new('RGB', (100, 200), 'green')
        image.paste('white', (1, 0, 6, 200))
        image.paste('black', (94, 0, 99, 200))
        image.getexif()[274] = 6
        self.assertEqual(thumbs.background_fields(image), {'bg': '181'})
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            archive = Path(temporary)
            month = archive / 'images/2024/01'
            month.mkdir(parents=True)
            image.save(month / 'rotated.jpg', 'JPEG', quality=100, exif=image.getexif())
            ingest.update_indexes(archive)
            saved = json.loads((month / 'entry_index.json').read_bytes())[0]
            self.assertEqual(saved, {'name': 'rotated.jpg', 'w': 200, 'h': 100, 'bg': '181'})

    def test_background_fields_average_the_cropped_image_in_all_orientations(self):
        for size in [(100, 100), (96, 100), (100, 96), (100, 200), (200, 100)]:
            image = Image.new('RGB', size, 'white')
            image.paste('red', (5, 5, size[0] - 5, size[1] - 5))
            with self.subTest(size=size):
                self.assertEqual(thumbs.background_fields(image),
                                 {'bg': thumbs.average_color(thumbs.preview_image(image))})
        self.assertEqual(thumbs.background_fields(Image.new('L', (1, 1), 255)), {'bg': 'FFF'})
        self.assertEqual(thumbs.background_fields(Image.new('RGB', (1, 2), 'red')), {'bg': 'F00'})


class PreviewCropTests(unittest.TestCase):
    def test_symmetric_one_percent_crop_at_source_resolution(self):
        for size, expected in [((500, 800), (490, 784)), ((743, 500), (729, 490)),
                               ((1, 2), (1, 2)), ((2, 1), (2, 1))]:
            with self.subTest(size=size):
                image = Image.new('RGB', size, 'red')
                self.assertEqual(thumbs.preview_image(image).size, expected)
                self.assertEqual(image.size, size)

    def test_frames_are_excluded_from_both_sprites_and_colors(self):
        for size in [(500, 800), (800, 500), (500, 500)]:
            with self.subTest(size=size):
                image = Image.new('RGB', size, 'black')
                image.paste('white', (3, 3, size[0] - 3, size[1] - 3))
                before = image.tobytes()
                self.assertEqual(thumbs.background_fields(image), {'bg': 'FFF'})
                tile = thumbs.make_tile(image)
                self.assertEqual(tile.crop(thumbs.tile_bounds(*size)).getextrema(), ((255, 255),) * 4)
                self.assertEqual(image.tobytes(), before)

    def test_original_ratio_controls_sprite_bounds(self):
        # Rounding the crop changes 95:100 to 93:98; retain original fitted bounds.
        image = Image.new('RGB', (95, 100), 'red')
        self.assertEqual(thumbs.background_fields(image), {'bg': 'F00'})
        tile = thumbs.make_tile(image)
        self.assertEqual(tile.getpixel((61, 60)), (0, 0, 0, 0))
        self.assertEqual(tile.getpixel((62, 60)), (255, 0, 0, 255))
        self.assertEqual(tile.getpixel((156, 159)), (255, 0, 0, 255))
        self.assertEqual(tile.getpixel((157, 159)), (0, 0, 0, 0))


class AverageColorTests(unittest.TestCase):
    def test_mixed_colors_are_averaged_not_counted(self):
        image = Image.new('RGB', (10, 1), 'black')
        image.paste('white', (0, 0, 4, 1))
        self.assertEqual(thumbs.average_color(image), '666')
        image = Image.new('RGB', (2, 1), 'red')
        image.putpixel((1, 0), (0, 0, 255))
        self.assertEqual(thumbs.average_color(image), '808')

    def test_rounds_only_after_averaging(self):
        image = Image.new('RGB', (2, 1), (0, 0, 0))
        image.putpixel((1, 0), (17, 18, 255))
        self.assertEqual(thumbs.average_color(image), '118')
        self.assertEqual(thumbs.average_color(Image.new('RGB', (1, 1), (8, 9, 128))), '018')

    def test_uses_every_original_pixel_without_downsampling(self):
        image = Image.new('RGB', (513, 257), 'black')
        image.paste('white', (0, 0, 171, 257))
        with patch.object(Image.Image, 'thumbnail', side_effect=AssertionError('image downsampled')):
            self.assertEqual(thumbs.average_color(image), '555')
            self.assertEqual(thumbs.background_fields(image), {'bg': '555'})

    def test_grayscale_palette_and_single_pixel_images(self):
        self.assertEqual(thumbs.average_color(Image.new('L', (1, 1), 255)), 'FFF')
        palette = Image.new('P', (2, 1), 0)
        palette.putpalette([255, 0, 0, 0, 0, 255] + [0] * 762)
        palette.putpixel((1, 0), 1)
        self.assertEqual(thumbs.average_color(palette), '808')


if __name__ == '__main__':
    unittest.main()
