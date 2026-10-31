"""Python 3.10+ / standard library only. Run: python nsp_extractor.py"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from dataclasses import dataclass


DEFAULT_TOOL = Path.home() / 'Documents/Desktop Folders/Tools/Nintendo Switch Tools/nstool-v1.9.2-win_x64/nstool.exe'
NSTOOL_CREDIT = 'NSTool by Jack (jakcron) | MIT License'
APP_NAME = 'NSP Unpacker'


@dataclass
class Content:
    path: Path
    kind: int
    index: int


@dataclass
class Title:
    title_id: int
    version: int
    kind: int
    application_id: int
    contents: list[Content]


def parse_cnmt(path: Path, ncas: dict[str, Path]) -> Title:
    data = path.read_bytes()
    if len(data) < 32:
        raise ValueError(f'Truncated metadata: {path.name}')
    title_id, version, kind = struct.unpack_from('<QIB', data)
    extra, count = struct.unpack_from('<HH', data, 14)
    if kind not in (0x80, 0x81, 0x82):
        raise ValueError(f'Unsupported title metadata type 0x{kind:02X} in {path.name}')
    start = 32 + extra
    if start + count * 56 > len(data):
        raise ValueError(f'Truncated content list: {path.name}')
    if kind != 0x80 and extra < 8:
        raise ValueError(f'Missing application ID: {path.name}')
    application_id = title_id if kind == 0x80 else struct.unpack_from('<Q', data, 32)[0]
    contents = []
    for offset in range(start, start + count * 56, 56):
        content_id = data[offset + 32:offset + 48].hex()
        content_kind, index = data[offset + 54:offset + 56]
        # Control/manual/legal/delta NCAs do not hold requested files.
        if content_kind not in (1, 2):
            continue
        if content_id not in ncas:
            raise ValueError(f'Missing content NCA {content_id}.nca ({path.name})')
        contents.append(Content(ncas[content_id], content_kind, index))
    return Title(title_id, version, kind, application_id, contents)


def newest(titles: list[Title]) -> list[Title]:
    selected: dict[int, Title] = {}
    for title in titles:
        if title.title_id not in selected or title.version > selected[title.title_id].version:
            selected[title.title_id] = title
    return list(selected.values())


class Extractor:
    def __init__(self, tool: Path, keys: Path, output: Path, log, cancelled: threading.Event):
        self.tool, self.keys, self.output = tool, keys, output
        self.log, self.cancelled = log, cancelled
        self.tickets: list[Path] = []
        self.work: Path | None = None

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise RuntimeError('Cancelled. Partial output and working files were retained.')

    def run(self, args: list[str | Path], source: Path, *, partition: bool = False):
        self.check_cancelled()
        command = [str(self.tool), '-k', str(self.keys)]
        for ticket in self.tickets:
            command += ['--tik', str(ticket)]
        command += [str(a) for a in args] + [str(source)]
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        # Poll without blocking the GUI; no shell and no keys in command output.
        with tempfile.TemporaryFile(mode='w+b') as capture:
            with subprocess.Popen(command, stdout=capture, stderr=subprocess.STDOUT,
                                  creationflags=flags) as process:
                while process.poll() is None:
                    if self.cancelled.wait(0.15):
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        self.check_cancelled()
                capture.seek(0)
                text = capture.read().decode('utf-8', errors='replace')
        # Do not log ordinary NCA output, which may include key-related fields.
        failed_partition = re.search(r'NCA Partition \d+ not readable', text, re.I)
        failed_extract = re.search(r'Failed to extract virtual path|Extract path was invalid', text, re.I)
        if process.returncode or '[ERROR]' in text or re.search(r'\bERROR\]', text) or failed_extract or (partition and failed_partition):
            safe_lines = [line for line in text.splitlines()
                          if ('error' in line.lower() or 'not readable' in line.lower() or '[WARNING]' in line)
                          and not re.search(r'[0-9a-fA-F]{32,}', line)]
            detail = '\n'.join(safe_lines[-6:]) or 'Check keys, tickets, source files, and free disk space.'
            raise RuntimeError(f'NSTool failed on {source.name} (exit {process.returncode}).\n{detail}')

    def dump_partition(self, content: Content, number: int, destination: Path, base: Content | None = None):
        destination.mkdir(parents=True, exist_ok=False)
        args: list[str | Path] = []
        if base:
            args += ['--basenca', base.path]
        args += ['-x', f'/{number}', destination]
        self.run(args, content.path, partition=True)
        if not any(p.is_file() for p in destination.rglob('*')):
            raise RuntimeError(f'No files extracted to {destination}. Check keys and selected content.')

    def extract(self, inputs: list[Path]):
        if not inputs:
            raise ValueError('Choose at least one NSP.')
        for path in [self.tool, self.keys, *inputs]:
            if not path.is_file():
                raise ValueError(f'File not found: {path}')
        if any(path.suffix.lower() != '.nsp' for path in inputs):
            raise ValueError('All inputs must be .nsp files (compressed .nsz files are unsupported).')
        if self.output.exists() and (not self.output.is_dir() or any(self.output.iterdir())):
            raise ValueError('Choose a new or empty output folder to avoid mixing dumps.')
        self.output.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix='_nsp_work_', dir=self.output))
        self.log('Extracting NSP containers. Keep enough free space for NCAs and final files.')
        folders = []
        for i, source in enumerate(inputs, 1):
            self.log(f'[{i}/{len(inputs)}] Unpacking {source.name}')
            folder = self.work / f'nsp-{i}'
            self.run(['-x', folder], source)
            folders.append(folder)
        self.tickets = [ticket for folder in folders for ticket in folder.rglob('*.tik')]
        ncas: dict[str, Path] = {}
        metas = []
        for folder in folders:
            for path in folder.rglob('*'):
                if path.suffix.lower() == '.nca':
                    ncas[path.name.split('.')[0].lower()] = path
                    if path.name.lower().endswith('.cnmt.nca'):
                        metas.append(path)
        if not metas:
            raise RuntimeError('No .cnmt.nca metadata found. These NSPs may be incomplete or unsupported.')
        titles = []
        for i, meta in enumerate(metas):
            self.log(f'Reading title metadata ({i + 1}/{len(metas)})')
            folder = self.work / f'meta-{i}'
            self.run(['-x', folder], meta, partition=True)
            cnmts = list(folder.rglob('*.cnmt'))
            if not cnmts:
                raise RuntimeError(f'No readable CNMT in {meta.name}. Check prod.keys.')
            titles.extend(parse_cnmt(path, ncas) for path in cnmts)
        titles = newest(titles)
        bases = {t.title_id: t for t in titles if t.kind == 0x80}
        patches = {t.application_id: t for t in titles if t.kind == 0x81}
        for app_id in patches:
            if app_id not in bases:
                raise ValueError(f'Update for {app_id:016X} needs its base-game NSP. Add it and retry.')
        results = []
        for app_id, base_title in bases.items():
            patch = patches.get(app_id)
            chosen = patch or base_title
            root = self.output if len(bases) == 1 else self.output / f'{app_id:016X}'
            programs = {c.index: c for c in base_title.contents if c.kind == 1}
            updates = {c.index: c for c in patch.contents if c.kind == 1} if patch else {}
            if not programs:
                raise ValueError(f'Base {app_id:016X} has no Program NCA.')
            if set(updates) - set(programs):
                raise ValueError('Update includes a program without matching base content.')
            self.log(f'Extracting game {app_id:016X}, version {chosen.version}' + (' (updated)' if patch else ' (base)'))
            for index, original in sorted(programs.items()):
                target = root if index == 0 else root / 'programs' / str(index)
                content = updates.get(index, original)
                base_content = original if index in updates else None
                self.log(f'  Program {index}: ExeFS')
                self.dump_partition(content, 0, target / 'exefs', base_content)
                if not (target / 'exefs' / 'main').is_file():
                    raise RuntimeError(f'Missing ExeFS main in {target}. Extraction is incomplete.')
                self.log(f'  Program {index}: RomFS (this can take a while)')
                self.dump_partition(content, 1, target / 'romfs', base_content)
            results.append({'title_id': f'{app_id:016X}', 'version': chosen.version, 'kind': 'game', 'folder': str(root)})
        for dlc in (t for t in titles if t.kind == 0x82):
            root = self.output
            if len(bases) > 1:
                root /= f'{dlc.application_id:016X}'
            root = root / 'DLC' / f'{dlc.title_id:016X}'
            contents = [c for c in dlc.contents if c.kind == 2]
            if not contents:
                self.log(f'DLC {dlc.title_id:016X}: no Data NCA; entitlement only, no RomFS to extract.')
                results.append({'title_id': f'{dlc.title_id:016X}', 'version': dlc.version,
                                'kind': 'dlc', 'status': 'no extractable filesystem'})
                continue
            self.log(f'Extracting DLC {dlc.title_id:016X}')
            for content in contents:
                target = root if len(contents) == 1 else root / f'content-{content.index}'
                self.dump_partition(content, 0, target / 'romfs')
            results.append({'title_id': f'{dlc.title_id:016X}', 'version': dlc.version, 'kind': 'dlc', 'folder': str(root)})
        self.check_cancelled()
        if not results:
            raise ValueError('No base game or DLC content found.')
        (self.output / 'extraction.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        # Only delete the unique working directory created by this invocation.
        shutil.rmtree(self.work)
        self.log(f'Done. Files are in {self.output}')


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry('820x690')
        self.minsize(680, 560)
        self.messages: queue.Queue = queue.Queue()
        self.cancelled = threading.Event()
        self.running = False
        local_tool = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent / 'nstool.exe'
        detected_tool = next((str(p) for p in (local_tool, DEFAULT_TOOL) if p.is_file()), shutil.which('nstool') or '')
        self.tool = tk.StringVar(value=detected_tool)
        candidates = [Path.home() / '.switch/prod.keys', DEFAULT_TOOL.parent / 'prod.keys',
                      Path.home() / 'AppData/Roaming/Ryujinx/system/prod.keys',
                      Path.home() / 'AppData/Roaming/yuzu/keys/prod.keys']
        self.keys = tk.StringVar(value=next((str(p) for p in candidates if p.is_file()), ''))
        self.output = tk.StringVar()
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(1, weight=1)
        ttk.Label(outer, text='Extract NSP game files', font=('Segoe UI', 16, 'bold')).grid(row=0, column=0, columnspan=3, sticky='w')
        ttk.Label(outer, text='Add base game, updates, and DLC together. The newest supplied update is applied automatically.').grid(row=1, column=0, columnspan=3, sticky='w', pady=(4, 12))
        for row, label, variable, directory in [(2, 'NSTool executable', self.tool, False),
                                                (3, 'prod.keys', self.keys, False),
                                                (4, 'Output folder', self.output, True)]:
            ttk.Label(outer, text=label).grid(row=row, column=0, sticky='w', padx=(0, 10), pady=4)
            ttk.Entry(outer, textvariable=variable).grid(row=row, column=1, sticky='ew', pady=4)
            ttk.Button(outer, text='Browse…', command=lambda v=variable, d=directory: self.browse(v, d)).grid(row=row, column=2, padx=(8, 0))
        ttk.Label(outer, text='NSP paths — one per line (quoted paths are accepted)').grid(row=5, column=0, columnspan=3, sticky='w', pady=(14, 5))
        self.paths = tk.Text(outer, height=7, wrap='none', font=('Consolas', 10))
        self.paths.grid(row=6, column=0, columnspan=3, sticky='nsew')
        outer.rowconfigure(6, weight=1)
        buttons = ttk.Frame(outer)
        buttons.grid(row=7, column=0, columnspan=3, sticky='ew', pady=8)
        ttk.Button(buttons, text='Add NSP files…', command=self.add_files).pack(side='left')
        ttk.Button(buttons, text='Clear list', command=lambda: self.paths.delete('1.0', 'end')).pack(side='left', padx=8)
        self.start_button = ttk.Button(buttons, text='Extract', command=self.start)
        self.start_button.pack(side='right')
        self.cancel_button = ttk.Button(buttons, text='Cancel', command=self.cancelled.set, state='disabled')
        self.cancel_button.pack(side='right', padx=8)
        self.progress = ttk.Progressbar(outer, mode='indeterminate')
        self.progress.grid(row=8, column=0, columnspan=3, sticky='ew', pady=(0, 8))
        self.log_box = tk.Text(outer, height=10, wrap='word', state='disabled', font=('Consolas', 10))
        self.log_box.grid(row=9, column=0, columnspan=3, sticky='nsew')
        outer.rowconfigure(9, weight=1)
        ttk.Label(outer, text='Choose an empty output folder. DLC stays separate. Failed/cancelled runs retain working files.').grid(row=10, column=0, columnspan=3, sticky='w', pady=(8, 0))
        ttk.Label(outer, text=NSTOOL_CREDIT).grid(row=11, column=0, columnspan=2, sticky='w', pady=(6, 0))
        ttk.Button(outer, text='Credits & licenses', command=self.credits).grid(row=11, column=2, sticky='e', pady=(6, 0))
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(100, self.poll)

    def browse(self, variable, directory):
        value = filedialog.askdirectory(title='Choose an empty output folder') if directory else filedialog.askopenfilename()
        if value:
            variable.set(value)

    def credits(self):
        window = tk.Toplevel(self)
        window.title('Credits & licenses')
        window.geometry('720x480')
        box = tk.Text(window, wrap='word', padx=12, pady=12)
        scroll = ttk.Scrollbar(window, command=box.yview)
        box.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        box.pack(fill='both', expand=True)
        box.insert('end', 'NSP Unpacker\nCopyright (c) 2026 adombs\nLicensed under the MIT License.\n\n'
                   'Extraction powered by Nintendo Switch Tool (NSTool).\n'
                   'Original author: Jack (jakcron)\nhttps://github.com/jakcron/nstool\n\n'
                   'This application provides a GUI and automation around the separate NSTool executable.\n'
                   'It is an independent project; no endorsement by the NSTool author is implied.\n\n')
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        own_license = root / 'LICENSE'
        if own_license.is_file():
            box.insert('end', '--- NSP Unpacker license ---\n\n' + own_license.read_text(encoding='utf-8') + '\n\n')
        for license_file in sorted((root / 'licenses').glob('*.txt')):
            box.insert('end', f'--- {license_file.name} ---\n\n' + license_file.read_text(encoding='utf-8') + '\n\n')
        box.configure(state='disabled')

    def add_files(self):
        files = filedialog.askopenfilenames(title='Select base, update, and DLC NSPs', filetypes=[('NSP files', '*.nsp')])
        for file in files:
            self.paths.insert('end', file + '\n')

    def start(self):
        if self.running:
            return
        fields = [self.tool.get(), self.keys.get(), self.output.get()]
        if not all(s.strip() for s in fields):
            messagebox.showerror('Missing paths', 'Choose NSTool, prod.keys, and an output folder.')
            return
        paths = list(dict.fromkeys(Path(line.strip().strip('"').strip("'")).expanduser().resolve()
                                  for line in self.paths.get('1.0', 'end').splitlines() if line.strip()))
        tool, keys, output = [Path(s.strip().strip('"')).expanduser().resolve() for s in fields]
        self.running = True
        self.cancelled.clear()
        self.start_button.configure(state='disabled')
        self.cancel_button.configure(state='normal')
        self.progress.start()

        def worker():
            extractor = Extractor(tool, keys, output, lambda text: self.messages.put(('log', text)), self.cancelled)
            try:
                extractor.extract(paths)
            except Exception as error:
                detail = str(error)
                if extractor.work:
                    detail += f'\nWorking files retained: {extractor.work}'
                self.messages.put(('error', detail))
            else:
                self.messages.put(('done', str(output)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, text = self.messages.get_nowait()
                self.log_box.configure(state='normal')
                self.log_box.insert('end', text + '\n')
                self.log_box.see('end')
                self.log_box.configure(state='disabled')
                if kind in ('error', 'done'):
                    self.running = False
                    self.progress.stop()
                    self.start_button.configure(state='normal')
                    self.cancel_button.configure(state='disabled')
                    if kind == 'error':
                        messagebox.showerror('Extraction stopped', text)
                    else:
                        messagebox.showinfo('Extraction complete', f'RomFS / ExeFS extraction complete.\n\n{text}')
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def close(self):
        if self.running:
            if messagebox.askyesno('Cancel extraction?', 'Cancel the current extraction? Close the window after it stops.'):
                self.cancelled.set()
        else:
            self.destroy()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--smoke-test':
        report = {}
        try:
            app = App()
            app.withdraw()
            app.update()
            report = {'gui_started': True, 'nstool_detected': bool(app.tool.get())}
            app.credits()
            app.update()
            license_root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
            license_text = (license_root / 'licenses/NSTool-MIT.txt').read_text(encoding='utf-8')
            report['nstool_credit_preserved'] = 'Copyright (c) 2019 Jack' in license_text
            app.destroy()
        except Exception as error:
            report = {'gui_started': False, 'error': str(error)}
        Path(sys.argv[2]).write_text(json.dumps(report, indent=2), encoding='utf-8')
        sys.exit(0 if report.get('gui_started') else 1)
    else:
        App().mainloop()
