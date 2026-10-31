"""Progress is measured from extraction order, not elapsed time or guessed byte totals."""
from pathlib import Path
import tempfile
import threading
import unittest

from nsp_extractor import App, DEFAULT_TOOL, Extractor, FileProgress, tree_files
from test_nsp_extractor import FakeExtractor


TREE = '''[NCA Header]
  Content Type: Program
[ContentArchive/Tree]
 program:/
  0/
   main
   main.npdm
  1/
   DATA0.bin
   a file with spaces.bin
   patch4/
    nested.bin
[ContentArchive/Extract]
Saving ignored.bin...
'''


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_scopes_tree_to_partition_and_preserves_order(self):
        self.assertEqual(tree_files(TREE, 1), [Path('DATA0.bin'), Path('a file with spaces.bin'), Path('patch4/nested.bin')])
        self.assertEqual(tree_files(TREE, 0), [Path('main'), Path('main.npdm')])

    def test_tree_all_partitions(self):
        self.assertEqual(tree_files(TREE)[-1], Path('1/patch4/nested.bin'))

    def test_invalid_tree_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'readable filesystem tree'):
            tree_files('No tree here')
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            tree_files('[PFS0/Tree]\n pfs:/\n  ../\n   escaped.bin\n')

    def test_last_file_stays_pending_until_success(self):
        events = []
        tracker = FileProgress(self.root, [Path('a'), Path('b')], 'RomFS', events.append)
        tracker.poll()
        self.assertEqual(events[-1]['completed'], 0)
        (self.root / 'a').write_bytes(b'1234')
        tracker.poll()
        self.assertEqual(events[-1]['completed'], 0)
        self.assertEqual(events[-1]['current_bytes'], 4)
        (self.root / 'b').write_bytes(b'12')
        tracker.poll()
        self.assertEqual(events[-1]['completed'], 1)
        self.assertEqual(events[-1]['current'], 'b')
        tracker.finish()
        self.assertEqual(events[-1]['completed'], 2)

    def test_missing_output_never_reports_complete(self):
        events = []
        tracker = FileProgress(self.root, [Path('a'), Path('b')], 'RomFS', events.append)
        (self.root / 'a').touch()
        tracker.poll()
        with self.assertRaisesRegex(RuntimeError, 'Expected files are missing'):
            tracker.finish()
        self.assertTrue(all(e['completed'] < e['total'] for e in events))

    def test_current_file_size_updates_while_percent_stays_honest(self):
        events = []
        tracker = FileProgress(self.root, [Path('large.bin')], 'RomFS', events.append)
        (self.root / 'large.bin').write_bytes(b'x')
        tracker.poll()
        (self.root / 'large.bin').write_bytes(b'xxx')
        tracker.poll()
        self.assertEqual([e['current_bytes'] for e in events], [1, 3])
        self.assertEqual([e['completed'] for e in events], [0, 0])

    def test_entire_fake_extraction_finishes_at_100(self):
        runner = FakeExtractor(self.root)
        events = []
        runner.progress = events.append
        runner.extract([runner.add('base.nsp', 0x1000, 0x80)])
        self.assertEqual(events[-1]['stage'], 'Complete')
        self.assertEqual(events[-1]['completed'], events[-1]['total'])
        self.assertTrue(any(e['stage'].startswith('RomFS') for e in events))

    def test_real_nstool_count_and_extract(self):
        if not DEFAULT_TOOL.is_file():
            self.skipTest('Local NSTool unavailable')
        import struct
        names = b'one.bin\0two.bin\0'
        source = self.root / 'source.nsp'
        header = struct.pack('<4sIII', b'PFS0', 2, len(names), 0)
        entries = struct.pack('<QQII', 0, 3, 0, 0) + struct.pack('<QQII', 3, 2, 8, 0)
        source.write_bytes(header + entries + names + b'abcde')
        keys = self.root / 'empty.keys'
        keys.touch()
        events = []
        runner = Extractor(DEFAULT_TOOL, keys, self.root / 'out', lambda _: None, threading.Event(), events.append)
        runner.extract_files(source, runner.output, 'NSP')
        self.assertEqual((runner.output / 'one.bin').read_bytes(), b'abc')
        self.assertEqual((runner.output / 'two.bin').read_bytes(), b'de')
        self.assertEqual(events[-1]['total'], 2)
        self.assertEqual(events[-1]['completed'], 2)

    def test_gui_percentage_never_rounds_pending_file_to_100(self):
        app = App()
        try:
            app.withdraw()
            app.show_progress({'stage': 'RomFS', 'total': 20000, 'completed': 19999,
                               'current': 'large.bin', 'current_bytes': 1048576})
            self.assertEqual(app.percent.get(), '99.9%')
            self.assertIn('files completed', app.progress_status.get())
            self.assertIn('1.0 MiB', app.current_file.get())
            app.show_progress({'stage': 'Reading metadata', 'total': None})
            self.assertEqual(app.percent.get(), '...')
        finally:
            app.destroy()


if __name__ == '__main__':
    unittest.main()
