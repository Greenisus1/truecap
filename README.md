# truecap

An offline USB write/read checker for Raspberry Pi / DietPi and other Linux systems.
Python 3.8 or newer, standard library only. No pip packages, GUI, or network required.
Not automatically installed on your Pi. Repository release version: 1.0.0
(the second development build, including drive listing).

## Before starting: back up everything

Use an EMPTY, expendable USB drive. Not your boot card, Ollama/model drive, or any
other drive holding something you want to keep. A counterfeit drive can wrap later
writes onto earlier data or filesystem metadata. Merely writing new test files can
destroy existing files. This tool cannot make a defective device safe.

It hides the entire disks backing the root/system filesystem or any /boot mount,
and refuses other partitions on those same disks as test targets. It also refuses
non-USB/non-removable targets, non-mounted directories,
and drives containing files (except an empty `lost+found`). Those checks do NOT
identify USB hardware or guarantee you chose the right device. Check the target.
It does not format drives and does not touch raw devices like `/dev/sda`.

## AppStore support

This repository includes `app-store.sh` with the Pi AppStore marker and
`app-version.json` version 1.0.0. Installation only checks Python and prints
readiness. AppStore Run lists eligible drives; it NEVER starts a write test
without a selected path and confirmation. To test, use the command below from
the installed app folder. A public repository is required for the current
public-only AppStore catalogue; visibility does not verify installation on a Pi.

## Run it

Copy `truecap.py` to your Pi. Run it without a path to list drives, without writing:

```
python3 truecap.py
```

Or use `python3 truecap.py --list`. Each eligible USB/removable volume shows:

- Device node (for example `/dev/sda1`).
- Drive name/model from sysfs and volume label when available.
- Every current mount path, or `(not mounted)`.
- USB bus/port topology identifier, for example `1-1.2`.

"Port" means the Linux USB bus/port topology, NOT a labelled physical socket on
the Pi case. Hub chains appear in this identifier. Non-USB removable cards show
`(not USB / unknown)`. Missing volume labels show `(unlabelled)` rather than a
made-up name. Labels come from `/dev/disk/by-label`; sysfs alone does not supply
filesystem labels. No `lsusb`, `udevadm`, or other command is called.

Non-removable internal disks are excluded unless they have USB topology (USB SSDs
can report non-removable). Regardless of USB/removable status, entire root/boot
backing disks and their partitions are hidden, including a USB boot drive. Device
mapper/RAID slaves are followed when represented by sysfs. If the system disk
cannot be resolved safely, listing/testing stops rather than risk showing it.
Unusual storage stacks, containers, or unsupported mount layouts may therefore
be refused. This listing is not hardware certification. The USB containing your
Ollama models may still appear if it is separate from root/boot, but do NOT test
it while it contains data.

Find your USB mount point with the listing. Optional independent cross-check:

```
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
```

Replace `/mnt/my-usb` with the actual root of the mounted USB filesystem:

```
python3 truecap.py /mnt/my-usb
```

Read the warnings, then type `yes` to start. There are no quotes or pipes in these
commands. If a mount path contains spaces, use the shell's normal path quoting.
DietPi root needs no sudo. A regular Linux user needs write access to the drive.

Do not unplug the drive, change its mount, or run other programs writing to it.
Make sure stable power is available. Other services that need this drive may fail
while it is full. Large, slow drives can take hours. The test writes the available
space once and reads it back, adding some wear to the flash.

To accept the same warnings without an interactive prompt:

```
python3 truecap.py /mnt/my-usb --yes
```

A bounded test (NOT a whole-drive capacity test):

```
python3 truecap.py /mnt/my-usb --limit-mib 1024
```

Files normally contain 256 MiB each. Optional `--file-mib` accepts 1 to 1024.
The tool works in 1 MiB memory blocks, so it doesn't need to hold the drive's
contents in RAM. It never stores or previews existing user files.

## What full mode actually does

1. Creates a uniquely named `.truecap-...` directory on the selected drive.
2. Writes new, offset-specific SHAKE-256 deterministic patterns, seeded differently
   for each run, into new test files until the filesystem says it is full.
3. Calls `fsync` after each file, then reads ALL recorded test blocks and compares
   them with the expected pattern. On Linux, aligned direct reads bypass the OS
   page cache where supported. Other reads use best-effort cache eviction.
4. Reports written bytes, bytes in fully matching blocks, failed-block bytes,
   the first bad byte's logical test/file offset, and any write/read errors.
5. Deletes ONLY the files it created and removes its own empty session directory.

Partial writes at the end of a full drive are included and checked. If a block
has even one bad byte, the whole block is excluded from the verified-byte count.
This is conservative: it is not a count of individually correct bytes.

`fsync` asks the OS/device to flush writes. It cannot force dishonest firmware to
persist data, and does not establish survival across a power cycle. If direct
reads are unavailable, cached reads can make a pass less conclusive. The program
prints that limitation when it occurs. It never globally drops system caches.

## Quick mode: very limited, not capacity proof

```
python3 truecap.py /mnt/my-usb --quick
```

Writes five 1 MiB samples at file offsets 0, 8, 16, 32, and 63 MiB in ONE sparse
64 MiB logical file, flushes, and reads them back. Only 5 MiB of test data is
written. Some filesystems allocate more space for sparse gaps.

This can spot obvious read/write failures quickly. It does NOT probe the far end
of the drive, test a claimed 1 TB, or measure physical capacity. Logical file
offsets do not select physical flash addresses; writing a sample far into a
sparse file is not an honest shortcut to proving the advertised capacity.
A fake 1 TB drive with 32 GB of working space can easily pass this quick test.
Use full mode for a stronger test. This version intentionally has no destructive
raw-device probing mode.

## Reading the result

- Full-mode match after reaching full: the tested available space returned the
  expected bytes during this run. This is stronger evidence than quick mode,
  not a guarantee of future reliability or exact physical capacity.
- Corruption or I/O error: the device failed the test. Counterfeit capacity is one
  possible cause. Damaged flash, an unstable connection, bad power, or filesystem
  problems can also cause failures. Don't trust the drive with important data.
- Bounded or quick match: only the written samples were checked, not the drive.
- No data tested: inconclusive.

The report's `verified_bytes` means bytes in fully matching test blocks. It is
NOT the drive's exact real/physical capacity. Filesystem overhead, reserved space,
allocation layout, failures, and controller behavior prevent that conclusion.
The first bad logical offset is NOT a reliable physical-size estimate either.

### What a fake drive looks like

A drive might claim "1 TB" to the operating system but only have a much smaller
amount of working flash. It may initially store small files correctly. As more
is written, the controller may discard data or overwrite earlier data. Later
readback can return zeros, changed bytes, I/O errors, or another block's data.
Seeing the expected advertised size in `lsblk` does not prove the space works.
This tool checks actual write/read behavior instead of trusting that size.
It does not decode how a particular counterfeit controller maps its flash.

## Cleanup and interruptions

Ctrl+C attempts to remove this run's test files. Errors also trigger cleanup.
Unplugging, power loss, `kill -9`, filesystem damage, or permissions errors may
leave the printed session directory behind. Do not blindly delete every hidden
directory: inspect the exact directory printed for this run. If cleanup cannot
complete, the program reports that and exits with status 3. It never recursively
deletes unknown files, even inside its session directory.

Exit codes: 0 = written blocks matched (scope/limits still apply), 1 = corruption
or I/O failure, 2 = cancelled or invalid arguments/target, 3 = incomplete/error
or cleanup failure, 130 = interrupted with successful cleanup.

## Local developer tests

Keep `test_truecap.py` beside `truecap.py` and run:

```
python3 -m unittest -v test_truecap
```

Tests cover deterministic patterns, local file writes/readback, forced corruption,
truncation, simulated wraparound, simulated full-device partial writes, I/O and
sync errors, quick samples, safe cleanup, interrupted cleanup, cancellation,
and target rejection. CLI filesystem safety validation is mocked ONLY inside
some developer tests; the shipped CLI has no bypass switch. Additional sysfs
fixture tests verify USB topology, model/label/mount decoding, unmounted drives,
internal/main-drive exclusion, USB boot exclusion, device-mapper backing-disk
exclusion, refusal of other main-disk partitions, and read-only listing.

These tests are local Linux tests, NOT a real counterfeit USB test or Pi hardware
certification. The supplied test-results.txt records this build's checks.

## Related established tool

F3 also fills media with pseudorandom data and reads it back. This is an independent,
small Python implementation, not F3 code and not a replacement for its raw-device
capacity analysis tools. Source: https://github.com/AltraMayor/f3
