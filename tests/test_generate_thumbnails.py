import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from scripts import generate_thumbnails as thumbs
from scripts import ingest_uploads


def entries(count):
    return [{"name": f"image {index:03d}.jpg", "w": 440, "h": 220, "x": 999, "y": 999}
            for index in range(count)]


def image_bytes(size=(440, 220), color="red", mode="RGB"):
    data = io.BytesIO()
    Image.new(mode, size, color).save(data, "PNG")
    return data.getvalue()


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "images/2024/01"
        self.image = image_bytes()

    def generate(self, index, source=None, **options):
        with contextlib.redirect_stdout(io.StringIO()):
            return thumbs.generate_month(index, self.output,
                source or (lambda name: io.BytesIO(self.image)), workers=2, **options)

    def test_geometry_and_partial_final_sheet(self):
        self.assertEqual(thumbs.tile_position(0), (0, 0))
        self.assertEqual(thumbs.tile_position(4), (888, 0))
        self.assertEqual(thumbs.tile_position(5), (0, 222))
        self.assertEqual(thumbs.tile_position(19), (888, 666))
        self.assertEqual(thumbs.tile_position(20), (0, 0))
        self.assertEqual(thumbs.sheet_name(100), "thumbnails-100.webp")
        self.assertEqual(self.generate(entries(41)), 3)
        for index, height in [(0, 886), (1, 886), (2, 220)]:
            with Image.open(self.output / thumbs.sheet_name(index)) as sheet:
                self.assertEqual(sheet.size, (1108, height))
                self.assertEqual(sheet.mode, "RGBA")
                self.assertEqual(sheet.getpixel((220, 110))[3], 0, 'gutters are transparent')
                self.assertEqual(sheet.format, "WEBP")
        index = json.loads((self.output / "entry_index.json").read_bytes())
        self.assertEqual(index, [{"name": item["name"], "w": 440, "h": 220} for item in entries(41)])
        self.assertEqual(len(list(self.output.iterdir())), 4)

    def test_exact_capacity_has_no_extra_row_or_sheet(self):
        self.assertEqual(self.generate(entries(20)), 1)
        with Image.open(self.output / "thumbnails-00.webp") as sheet:
            self.assertEqual(sheet.height, 886)
        self.assertFalse((self.output / "thumbnails-01.webp").exists())

    def test_source_order_and_not_legacy_coordinates_determine_sheet(self):
        index = entries(21)[::-1]
        red = image_bytes(color="red")
        blue = image_bytes(color="blue")
        self.generate(index, lambda name: io.BytesIO(blue if name == index[20]["name"] else red))
        with Image.open(self.output / "thumbnails-01.webp") as sheet:
            r, g, b, a = sheet.getpixel((110, 110))
            self.assertEqual(a, 255)
            self.assertGreater(b, 240)
            self.assertLess(r, 10)
        saved = json.loads((self.output / "entry_index.json").read_bytes())
        self.assertEqual([entry["name"] for entry in saved], [entry["name"] for entry in index])

    def test_fit_centering_no_upscale_and_grayscale_conversion(self):
        landscape = thumbs.make_tile(io.BytesIO(self.image))
        self.assertEqual(landscape.getpixel((110, 54)), (0, 0, 0, 0))
        self.assertEqual(landscape.getpixel((110, 55)), (255, 0, 0, 255))
        self.assertEqual(landscape.getpixel((110, 164)), (255, 0, 0, 255))
        self.assertEqual(landscape.getpixel((110, 165)), (0, 0, 0, 0))
        portrait = thumbs.make_tile(io.BytesIO(image_bytes(size=(220, 440))))
        self.assertEqual(portrait.getpixel((54, 110)), (0, 0, 0, 0))
        self.assertEqual(portrait.getpixel((55, 110)), (255, 0, 0, 255))
        small = thumbs.make_tile(io.BytesIO(image_bytes(size=(10, 10), color=255, mode="L")))
        self.assertEqual(small.mode, "RGBA")
        self.assertEqual(small.getpixel((104, 110)), (0, 0, 0, 0))
        self.assertEqual(small.getpixel((105, 110)), (255, 255, 255, 255))

    def test_source_alpha_is_preserved_without_squaring_it(self):
        image = Image.new('RGBA', (440, 220), (255, 0, 0, 128))
        tile = thumbs.make_tile(image)
        self.assertEqual(tile.getpixel((110, 110)), (255, 0, 0, 128))
        self.assertEqual(tile.getpixel((110, 54))[3], 0)
        self.generate(entries(1), lambda name: io.BytesIO(image_bytes(color=(255, 0, 0, 128), mode='RGBA')))
        with Image.open(self.output / 'thumbnails-00.webp') as sheet:
            self.assertEqual(sheet.getpixel((110, 110))[3], 128)
            self.assertEqual(sheet.getpixel((110, 54))[3], 0)
            self.assertEqual(sheet.getpixel((500, 110))[3], 0, 'unused cells are transparent')

    def test_unchanged_batches_resume_missing_sheet_and_force(self):
        index = entries(21)
        self.generate(index)
        source = lambda name: io.BytesIO(self.image)
        with patch.object(thumbs, "make_tile", wraps=thumbs.make_tile) as make:
            self.assertEqual(self.generate(index, source), 0)
            make.assert_not_called()
            (self.output / "thumbnails-01.webp").unlink()
            self.assertEqual(self.generate(index, source), 1)
            self.assertEqual(make.call_count, 1)
            self.assertEqual(self.generate(index, source, force=True), 2)
            self.assertEqual(make.call_count, 22)
        self.assertEqual(self.generate(entries(22)), 1)

    def test_background_metadata_updates_do_not_rebuild_sprites(self):
        index = [{**entries(1)[0], 'bg': 'FFF'}]
        self.generate(index)
        sprite = (self.output / 'thumbnails-00.webp').read_bytes()
        updated = [{**index[0], 'bg': 'F00', 'g': 'FFF000'}]
        self.assertEqual(self.generate(updated, lambda name: self.fail('original reopened')), 0)
        self.assertEqual((self.output / 'thumbnails-00.webp').read_bytes(), sprite)
        self.assertEqual(json.loads((self.output / 'entry_index.json').read_bytes()),
                         [{'name': index[0]['name'], 'w': 440, 'h': 220, 'bg': 'F00'}])

    def test_obsolete_edge_metadata_is_removed_without_opening_originals(self):
        index = [{**entries(1)[0], 'bg': 'ABC'}]
        self.generate(index)
        sprite = (self.output / 'thumbnails-00.webp').read_bytes()
        old_index = [{'name': index[0]['name'], 'w': 440, 'h': 220, 'bg': 'ABC', 'g': 'FFF000'}]
        (self.output / 'entry_index.json').write_text(json.dumps(old_index))
        self.assertEqual(self.generate(old_index, lambda name: self.fail('original reopened')), 0)
        self.assertEqual((self.output / 'thumbnails-00.webp').read_bytes(), sprite)
        self.assertEqual(json.loads((self.output / 'entry_index.json').read_bytes()),
                         [{'name': index[0]['name'], 'w': 440, 'h': 220, 'bg': 'ABC'}])

    def test_generated_sprite_padding_is_transparent_independent_of_bg(self):
        image = Image.new('RGB', (440, 220), 'white')
        image.paste('red', (11, 11, 429, 209))
        source = io.BytesIO()
        image.save(source, 'PNG')
        self.generate([{**entries(1)[0], 'bg': 'F00', 'g': 'FFF000'}],
                      lambda name: io.BytesIO(source.getvalue()))
        with Image.open(self.output / 'thumbnails-00.webp') as sheet:
            self.assertEqual(sheet.getpixel((110, 10))[3], 0)
            self.assertEqual(sheet.getpixel((110, 55))[3], 255)

    def test_failed_month_preserves_published_sheets_and_index(self):
        self.generate(entries(20))
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        def fail(name):
            if name == "image 020.jpg":
                raise OSError("download failed")
            return io.BytesIO(image_bytes(color="blue"))
        with self.assertRaisesRegex(RuntimeError, "image 020.jpg"):
            self.generate(entries(21), fail, force=True)
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, before)

    def test_removed_entries_remove_obsolete_sheets(self):
        self.generate(entries(41))
        self.assertEqual(self.generate(entries(20)), 0)
        self.assertEqual(sorted(path.name for path in self.output.iterdir()),
                         ["entry_index.json", "thumbnails-00.webp"])
        self.generate([])
        self.assertEqual(json.loads((self.output / "entry_index.json").read_bytes()), [])
        self.assertEqual(len(list(self.output.iterdir())), 1)

    def test_invalid_entries_and_sprite_exclusion(self):
        for name in ["../outside.jpg", "/absolute.jpg", "..", "bad\\name.jpg"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                thumbs.gallery_entries([{**entries(1)[0], "name": name}])
        with self.assertRaises(ValueError):
            thumbs.gallery_entries(entries(1) * 2)
        with self.assertRaises(ValueError):
            thumbs.gallery_entries([{**entries(1)[0], "w": 0}])
        self.assertEqual(thumbs.gallery_entries([
            {"name": name} for name in ["thumbnails.jpg", "thumbnails.webp", "thumbnails-00.jpg",
                                       "thumbnails-00.webp", "thumbnails-12-q50.webp"]
        ]), [])

    def test_quality_and_lossy_encoding(self):
        with patch.object(thumbs.Image.Image, "save", autospec=True) as save:
            thumbs.save_sheet(Image.new("RGB", (10, 10)), self.output / "sheet.webp")
        self.assertEqual(save.call_args.args[2], "WEBP")
        self.assertEqual(save.call_args.kwargs, {"quality": 55, "alpha_quality": 100, "method": 6, "lossless": False})

    def test_convert_existing_sheets_is_local_and_removes_jpegs(self):
        self.output.mkdir(parents=True)
        jpeg = self.output / "thumbnails-00.jpg"
        Image.new("RGB", (1108, 220), "red").save(jpeg, "JPEG")
        (self.output / "entry_index.json").write_text("[]")
        with patch.object(thumbs, "download", side_effect=AssertionError("must not download")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(thumbs.convert_existing_sheets(self.root / "images", workers=2), 1)
            self.assertEqual(thumbs.convert_existing_sheets(self.root / "images", workers=2), 0)
        self.assertFalse(jpeg.exists())
        with Image.open(jpeg.with_suffix(".webp")) as sheet:
            self.assertEqual(sheet.format, "WEBP")
            self.assertEqual(sheet.size, (1108, 220))
            self.assertTrue(jpeg.with_suffix(".webp").read_bytes().startswith(b"RIFF"))
            self.assertIn(b"VP8 ", jpeg.with_suffix(".webp").read_bytes()[:20])
        self.assertEqual((self.output / "entry_index.json").read_text(), "[]")

    def test_failed_conversion_preserves_jpeg_and_existing_webp(self):
        self.output.mkdir(parents=True)
        jpeg = self.output / "thumbnails-00.jpg"
        Image.new("RGB", (100, 100), "red").save(jpeg, "JPEG")
        previous = jpeg.with_suffix(".webp")
        previous.write_bytes(b"previous webp")
        before = jpeg.read_bytes()
        with patch.object(thumbs, "save_sheet", side_effect=OSError("disk full")), \
                self.assertRaisesRegex(OSError, "disk full"):
            thumbs.convert_existing_sheets(self.root / "images", workers=1)
        self.assertEqual(jpeg.read_bytes(), before)
        self.assertEqual(previous.read_bytes(), b"previous webp")
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_generation_removes_legacy_jpegs_only_after_success(self):
        self.output.mkdir(parents=True)
        jpeg = self.output / "thumbnails-00.jpg"
        jpeg.write_bytes(b"legacy jpeg")
        self.generate(entries(1))
        self.assertFalse(jpeg.exists())
        self.assertTrue(jpeg.with_suffix(".webp").exists())

    def test_archive_checkout_outputs_only_in_website(self):
        archive = self.root / "archive"
        directory = archive / "images/2024/01"
        directory.mkdir(parents=True)
        index = entries(21)
        (directory / "entry_index.json").write_text(json.dumps(index))
        for item in index:
            (directory / item["name"]).write_bytes(self.image)
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(thumbs.update_thumbnails(archive, self.root / "www"), 2)
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)
        local = self.root / "www/images/2024/01"
        self.assertEqual(sorted(path.name for path in local.iterdir()),
                         ["entry_index.json", "thumbnails-00.webp", "thumbnails-01.webp"])

    def test_remote_regeneration_downloads_originals_and_uses_local_index(self):
        directory_index = self.root / "dir_index.json"
        directory_index.write_text(json.dumps({"2024/01": 22, "2025/02": 1}))
        def fetch(url, **options):
            if url.endswith("entry_index.json"):
                return json.dumps(entries(21)).encode()
            self.assertIn("/images/2024/01/image%20", url)
            self.assertNotIn("thumbnails", url)
            return self.image
        with patch.object(thumbs, "download", side_effect=fetch) as download, contextlib.redirect_stdout(io.StringIO()):
            count = thumbs.regenerate_from_archive(directory_index, self.root / "images", ["2024/01"],
                workers=2, timeout=30, retries=0, force=False)
            self.assertEqual(count, 2)
            self.assertEqual(download.call_count, 22)
            self.assertEqual(download.call_args_list[0].args[0],
                "https://archiv0.derrosarotepanzer.com/images/2024/01/entry_index.json")
            self.assertEqual(json.loads(directory_index.read_bytes()), {"2024/01": 21, "2025/02": 1})
            thumbs.regenerate_from_archive(directory_index, self.root / "images", ["2024/01"],
                workers=2, timeout=30, retries=0, force=False)
            self.assertEqual(download.call_count, 23, "unchanged sheets must not re-download originals")
        with self.assertRaisesRegex(ValueError, "months not in directory index"):
            thumbs.regenerate_from_archive(directory_index, self.root / "images", ["2024/02"],
                workers=2, timeout=30, retries=0, force=False)

    def test_remote_background_refresh_preserves_existing_sheets(self):
        directory_index = self.root / 'dir_index.json'
        directory_index.write_text(json.dumps({'2024/01': 1}))
        index = [{**entries(1)[0], 'bg': '000', 'g': '000000'}]
        self.generate(index)
        sprite = (self.output / 'thumbnails-00.webp').read_bytes()
        image = Image.new('RGB', (440, 220), 'black')
        image.paste('white', (0, 0, 176, 220))
        source = io.BytesIO()
        image.save(source, 'PNG')
        def fetch(url, **options):
            return json.dumps(index).encode() if url.endswith('entry_index.json') else source.getvalue()
        with patch.object(thumbs, 'download', side_effect=fetch) as download, contextlib.redirect_stdout(io.StringIO()):
            count = thumbs.regenerate_from_archive(directory_index, self.root / 'images', [],
                workers=2, timeout=30, retries=0, force=False, refresh_backgrounds=True)
        self.assertEqual(count, 0)
        self.assertEqual(download.call_count, 2)
        self.assertEqual((self.output / 'thumbnails-00.webp').read_bytes(), sprite)
        saved = json.loads((self.output / 'entry_index.json').read_bytes())
        self.assertEqual(saved[0]['bg'], '666')
        self.assertNotIn('g', saved[0])
        # A remote index using dominant colors in the same schema cannot undo the refresh.
        with patch.object(thumbs, 'download', side_effect=fetch) as download:
            count = thumbs.regenerate_from_archive(directory_index, self.root / 'images', [],
                workers=2, timeout=30, retries=0, force=False)
        self.assertEqual(count, 0)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(json.loads((self.output / 'entry_index.json').read_bytes()), saved)

    def test_background_refresh_rebuilds_if_source_dimensions_were_stale(self):
        index = [{'name': 'image.jpg', 'w': 220, 'h': 440}]
        self.generate(index, lambda name: io.BytesIO(image_bytes(size=(220, 440), color='blue')))
        before = (self.output / 'thumbnails-00.webp').read_bytes()
        # The real source is landscape; even a colors-only refresh must correct
        # the sprite geometry together with the display dimensions.
        self.assertEqual(self.generate(index, force=False, refresh_backgrounds=True), 1)
        saved = json.loads((self.output / 'entry_index.json').read_bytes())[0]
        self.assertEqual((saved['w'], saved['h']), (440, 220))
        self.assertNotEqual((self.output / 'thumbnails-00.webp').read_bytes(), before)

    def test_forced_crop_rebuild_refreshes_colors_with_one_download_per_image(self):
        directory_index = self.root / 'dir_index.json'
        directory_index.write_text(json.dumps({'2024/01': 21}))
        index = [{**entry, 'bg': '000', 'g': '000000'} for entry in entries(21)]
        self.generate(index)
        image = Image.new('RGB', (440, 220), 'black')
        image.paste('white', (2, 2, 438, 218))
        source = io.BytesIO()
        image.save(source, 'PNG')
        def fetch(url, **options):
            return json.dumps(index).encode() if url.endswith('entry_index.json') else source.getvalue()
        with patch.object(thumbs, 'download', side_effect=fetch) as download, contextlib.redirect_stdout(io.StringIO()):
            count = thumbs.regenerate_from_archive(directory_index, self.root / 'images', [],
                workers=2, timeout=30, retries=0, force=True, refresh_backgrounds=True)
        self.assertEqual(count, 2)
        self.assertEqual(download.call_count, 22)
        saved = json.loads((self.output / 'entry_index.json').read_bytes())
        self.assertEqual(saved, [{'name': entry['name'], 'w': 440, 'h': 220,
                                 'bg': 'FFF'} for entry in index])
        with Image.open(self.output / 'thumbnails-00.webp') as sheet:
            self.assertTrue(all(channel > 245 for channel in sheet.getpixel((110, 55))))

    def test_failed_combined_refresh_does_not_publish_colors_or_sheets(self):
        index = [{**entry, 'bg': '000', 'g': '000000'} for entry in entries(21)]
        self.generate(index)
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        def source(name):
            if name == index[-1]['name']:
                raise OSError('missing original')
            return io.BytesIO(image_bytes(color='white'))
        with self.assertRaisesRegex(RuntimeError, 'missing original'):
            self.generate(index, source, force=True, refresh_backgrounds=True)
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, before)

    def test_cached_square_average_survives_remote_dominant_colors(self):
        directory_index = self.root / 'dir_index.json'
        directory_index.write_text(json.dumps({'2024/01': 1}))
        cached = [{'name': 'square.jpg', 'w': 220, 'h': 220, 'bg': '888'}]
        self.generate(cached)
        remote = [{**cached[0], 'bg': '000'}]
        with patch.object(thumbs, 'download', return_value=json.dumps(remote).encode()) as download:
            count = thumbs.regenerate_from_archive(directory_index, self.root / 'images', [],
                workers=2, timeout=30, retries=0, force=False)
        self.assertEqual(count, 0)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(json.loads((self.output / 'entry_index.json').read_bytes()), cached)

    def test_failed_background_refresh_preserves_index_and_sprites(self):
        directory_index = self.root / 'dir_index.json'
        directory_index.write_text(json.dumps({'2024/01': 1}))
        index = entries(1)
        self.generate(index)
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        def fetch(url, **options):
            if url.endswith('entry_index.json'):
                return json.dumps(index).encode()
            raise OSError('download failed')
        with patch.object(thumbs, 'download', side_effect=fetch), self.assertRaisesRegex(RuntimeError, 'download failed'):
            thumbs.regenerate_from_archive(directory_index, self.root / 'images', [],
                workers=2, timeout=30, retries=0, force=False, refresh_backgrounds=True)
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, before)

    def test_archive_index_does_not_count_sprites_as_originals(self):
        archive = self.root / "archive"
        directory = archive / "images/2024/01"
        directory.mkdir(parents=True)
        for name in ["original.jpg", "thumbnails.jpg", "thumbnails-00.jpg"]:
            (directory / name).write_bytes(self.image)
        with contextlib.redirect_stdout(io.StringIO()):
            ingest_uploads.update_indexes(archive)
        self.assertEqual(json.loads((archive / "images/dir_index.json").read_bytes()), {"2024/01": 1})
        self.assertEqual([entry["name"] for entry in json.loads((directory / "entry_index.json").read_bytes())], ["original.jpg"])


if __name__ == "__main__":
    unittest.main()
