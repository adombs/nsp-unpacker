# NSP Unpacker

A simple Windows GUI to extract RomFS and ExeFS from Nintendo Switch NSPs using NSTool.
Select multiple base-game, update, and DLC packages and an output folder. Updates are matched
automatically, and DLC files stay separate.

Download the Windows EXE from [Releases](https://github.com/adombs/nsp-unpacker/releases).
Download the external [NSTool executable](https://github.com/jakcron/nstool/releases) separately.

## License

NSP Unpacker is copyright (c) 2026 adombs and licensed under the [MIT License](LICENSE).
Third-party software retains its own licenses and notices.

## Credits and third-party licensing

Extraction is powered by **Nintendo Switch Tool (NSTool)**, originally authored by
**Jack (jakcron)**: https://github.com/jakcron/nstool.
NSTool is copyright (c) 2019 Jack and is licensed under the MIT License. Its full, unmodified
v1.9.2 license is preserved in [licenses/NSTool-MIT.txt](licenses/NSTool-MIT.txt).
This project supplies the GUI and automation; NSTool remains a separate external dependency.
This is an independent project and does not imply endorsement by its author.
The GUI's **Credits & licenses** window includes the third-party notices embedded in the EXE.

NSTool's MIT license permits publishing, modification, and redistribution, including commercial
use, provided its copyright and permission notices accompany copies or substantial portions.
If redistributing NSTool itself, also review the licenses of dependencies in that distributed build.
Runtime notices in `licenses/` apply to the packaged EXE.

Publish only the application source and intended release artifacts. Keep keys, tickets, game NSPs,
and extracted game files out of the repository. The open-source license does not grant rights to
distribute game content or keys or resolve local anti-circumvention rules.

## Running

Windows executable: `NSP-Unpacker.exe`. Double-click it; Python is bundled.
NSTool remains external: put `nstool.exe` next to the EXE, put it on PATH, or select it with Browse.

Run `python nsp_extractor.py` with Python 3.10 or newer. No pip packages are required.
The standard Python Windows installer includes Tkinter, which supplies the GUI.

1. Select `nstool.exe` and your `prod.keys`. Common local paths are detected automatically.
2. Add multiple NSPs with **Add NSP files**, or paste full paths, one per line. Quoted paths work.
3. Choose a new or empty output directory and click **Extract**.

The progress bar shows the percentage of files completed in the **current stage**, such as
unpacking an NSP or extracting RomFS. It also shows the current filename and MiB written.
Large files can keep the percentage steady while their MiB counter increases: this is file-count
progress, not a byte percentage or a time estimate. Counting files and reading metadata use an
activity indicator until a file total is available. The stage reaches 100% after NSTool succeeds
and all expected output files are present.

Supply the base NSP together with any update NSPs. The script reads CNMT metadata to identify
base games, updates, and DLC, and chooses the highest version supplied for each title.
All input tickets are passed to NSTool, including the base ticket when extracting an update.
DLC can also be extracted without a base NSP. Inputs must be standard NSPs, not NSZs.

For one base game the output is:

```text
output/
  exefs/                  # updated executable files, including main
  romfs/                  # updated game filesystem
  DLC/<title-id>/romfs/    # separate filesystem for each supplied DLC
  extraction.json         # title IDs, selected versions, and locations
```

Multiple base games get their own title-ID folders. Additional program indices go under
`programs/<index>/`. DLC with multiple Data NCAs has `content-<index>/romfs/` subfolders.
DLC typically contains only RomFS, so it does not get an ExeFS folder.
Entitlement-only DLC has no filesystem; it is noted in the log and manifest without stopping the batch.

Keep enough free space for the unpacked NCAs plus the extracted files. Temporary NCAs are
deleted after success. On failure or cancellation the working directory is retained inside
the output folder for inspection; choose a fresh output folder for the next run.
Your NSPs are read and remain untouched. Updates require their matching base game.

NSTool 1.9.2's extraction and ticket options were checked against its built-in help and
[official source](https://github.com/jakcron/nstool/tree/v1.9.2).
This wrapper does not provide keys or game files. Unsupported/incomplete metadata, unreadable
partitions, or missing outputs cause an error rather than a success message.

Validation: `python -m unittest -v` uses synthetic metadata/content and a fake extraction backend;
it also checks GUI startup, false-success warnings, and real NSTool extraction of a synthetic NSP
when the local executable is available. Full encrypted NSP extraction requires your own inputs.

## Building

On Windows with Python 3.10 or newer:

```powershell
python -m venv .build-venv
.\.build-venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.build-venv\Scripts\python.exe -m PyInstaller --noconfirm NSP-Unpacker.spec
```

The output is `dist/NSP-Unpacker.exe`. The build bundles Python, Tkinter, and license notices;
it does not bundle NSTool, keys, or game files.
Packaged startup check: `NSP-Unpacker.exe --smoke-test report.json`.
