"""Synthetic tests: no game data or keys required. python -m unittest -v"""
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from nsp_extractor import App, Extractor, parse_cnmt


def metadata(title_id, kind, content_id, version=0, app_id=0x1000, index=0):
    extra = 16
    blob = bytearray(32 + extra + 56)
    struct.pack_into('<QIB', blob, 0, title_id, version, kind)
    struct.pack_into('<HH', blob, 14, extra, 1)
    struct.pack_into('<Q', blob, 32, app_id)
    offset = 32 + extra
    blob[offset + 32:offset + 48] = bytes.fromhex(content_id)
    blob[offset + 54] = 2 if kind == 0x82 else 1
    blob[offset + 55] = index
    return blob


class FakeExtractor(Extractor):
    def __init__(self, root, output='out'):
        super().__init__(root / 'nstool.exe', root / 'prod.keys', root / output, lambda _: None, threading.Event())
        self.tool.touch()
        self.keys.touch()
        self.calls = []
        self.specs = {}

    def add(self, name, title_id, kind, version=0):
        path = self.output.parent / name
        path.touch()
        number = len(self.specs) + 1
        content_id = f'{number:032x}'
        self.specs[name] = (metadata(title_id, kind, content_id, version), content_id)
        return path

    def run(self, args, source, *, partition=False):
        self.check_cancelled()
        self.calls.append((args, source))
        target = Path(args[-1])
        target.mkdir(parents=True, exist_ok=True)
        if source.suffix == '.nsp':
            blob, content_id = self.specs[source.name]
            (target / f'{content_id}.nca').touch()
            (target / f'{content_id}.cnmt.nca').write_bytes(blob)
            (target / f'{content_id}.tik').touch()
        elif source.name.endswith('.cnmt.nca'):
            (target / 'title.cnmt').write_bytes(source.read_bytes())
        else:
            (target / ('main' if target.name == 'exefs' else 'synthetic.bin')).write_bytes(b'fixture')


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_base_update_and_dlc(self):
        runner = FakeExtractor(self.root)
        base = runner.add('base.nsp', 0x1000, 0x80)
        update = runner.add('update.nsp', 0x1800, 0x81, 2)
        dlc = runner.add('dlc.nsp', 0x2001, 0x82)
        runner.extract([update, dlc, base])
        self.assertTrue((runner.output / 'exefs/main').is_file())
        self.assertTrue((runner.output / 'romfs/synthetic.bin').is_file())
        self.assertTrue((runner.output / 'DLC/0000000000002001/romfs/synthetic.bin').is_file())
        self.assertEqual(len([a for a, _ in runner.calls if '--basenca' in a]), 2)
        self.assertEqual(len(runner.tickets), 3)
        self.assertFalse(runner.work.exists())

    def test_newest_update_selected(self):
        runner = FakeExtractor(self.root)
        inputs = [runner.add('base.nsp', 0x1000, 0x80), runner.add('new.nsp', 0x1800, 0x81, 5),
                  runner.add('old.nsp', 0x1800, 0x81, 1)]
        runner.extract(inputs)
        used = [source.name for args, source in runner.calls if '--basenca' in args]
        self.assertEqual(used, [f'{2:032x}.nca'] * 2)

    def test_update_needs_base(self):
        runner = FakeExtractor(self.root)
        with self.assertRaisesRegex(ValueError, 'base-game NSP'):
            runner.extract([runner.add('update.nsp', 0x1800, 0x81)])
        self.assertTrue(runner.work.exists())

    def test_dlc_only(self):
        runner = FakeExtractor(self.root)
        runner.extract([runner.add('dlc.nsp', 0x2001, 0x82)])
        self.assertFalse((runner.output / 'exefs').exists())
        self.assertTrue((runner.output / 'DLC/0000000000002001/romfs').is_dir())

    def test_entitlement_only_dlc(self):
        runner = FakeExtractor(self.root)
        path = runner.add('dlc.nsp', 0x2001, 0x82)
        blob, content_id = runner.specs[path.name]
        struct.pack_into('<H', blob, 16, 0)
        runner.specs[path.name] = (blob[:48], content_id)
        runner.extract([path])
        self.assertIn('no extractable filesystem', (runner.output / 'extraction.json').read_text())

    def test_real_nstool_extracts_synthetic_nsp_with_spaces(self):
        from nsp_extractor import DEFAULT_TOOL
        if not DEFAULT_TOOL.is_file():
            self.skipTest('Local NSTool unavailable')
        name = b'fixture.txt\0'
        payload = b'synthetic container fixture'
        header = struct.pack('<4sIII', b'PFS0', 1, len(name), 0)
        entry = struct.pack('<QQII', 0, len(payload), 0, 0)
        source = self.root / 'synthetic input.nsp'
        source.write_bytes(header + entry + name + payload)
        target = self.root / 'output with spaces'
        keys = self.root / 'empty.keys'
        keys.touch()
        runner = Extractor(DEFAULT_TOOL, keys, target, lambda _: None, threading.Event())
        runner.run(['-x', target], source)
        self.assertEqual((target / 'fixture.txt').read_bytes(), payload)

    def test_zero_exit_unreadable_partition_is_failure(self):
        runner = FakeExtractor(self.root)
        with patch('nsp_extractor.subprocess.Popen') as popen:
            process = popen.return_value.__enter__.return_value
            process.poll.return_value = 0
            process.returncode = 0
            def capture_output(*args, **kwargs):
                kwargs['stdout'].write(b'[WARNING] NCA Partition 1 not readable.\n')
                return popen.return_value
            popen.side_effect = capture_output
            with self.assertRaisesRegex(RuntimeError, 'not readable'):
                Extractor.run(runner, ['-x', '/1', self.root / 'romfs'], self.root / 'fake.nca', partition=True)

    def test_nonempty_output_preserved(self):
        runner = FakeExtractor(self.root)
        runner.output.mkdir()
        sentinel = runner.output / 'keep.txt'
        sentinel.write_text('keep')
        with self.assertRaisesRegex(ValueError, 'empty output'):
            runner.extract([runner.add('base.nsp', 0x1000, 0x80)])
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_missing_content_rejected(self):
        path = self.root / 'test.cnmt'
        path.write_bytes(metadata(0x1000, 0x80, 'aa' * 16))
        with self.assertRaisesRegex(ValueError, 'Missing content'):
            parse_cnmt(path, {})

    def test_truncated_metadata_rejected(self):
        path = self.root / 'test.cnmt'
        path.write_bytes(metadata(0x1000, 0x80, 'aa' * 16)[:-1])
        with self.assertRaisesRegex(ValueError, 'Truncated'):
            parse_cnmt(path, {})

    def test_cancellation(self):
        runner = FakeExtractor(self.root)
        runner.cancelled.set()
        with self.assertRaisesRegex(RuntimeError, 'Cancelled'):
            runner.extract([runner.add('base.nsp', 0x1000, 0x80)])

    def test_gui_starts_and_reads_multiple_quoted_paths(self):
        app = App()
        try:
            app.withdraw()
            app.update()
            app.paths.insert('end', '"C:/example/base.nsp"\n"C:/example/update.nsp"\n')
            app.keys.set(str(self.root / 'prod.keys'))
            app.tool.set(str(self.root / 'nstool.exe'))
            app.output.set(str(self.root / 'output'))
            with patch('nsp_extractor.threading.Thread') as thread:
                app.start()
                self.assertTrue(app.running)
                thread.return_value.start.assert_called_once()
        finally:
            app.running = False
            app.destroy()


if __name__ == '__main__':
    unittest.main()
