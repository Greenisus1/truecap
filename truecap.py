#!/usr/bin/env python3
"""Disposable mounted-volume write/read tester. Python 3 standard library only."""
import argparse
import errno
import hashlib
import json
import mmap
import os
import re
from pathlib import Path
import shutil
import sys
import tempfile
import time

MIB = 1024 * 1024
BLOCK = MIB


def pattern(seed, offset, size):
    """Unique, deterministic, incompressible-looking bytes for each test offset."""
    return hashlib.shake_256(seed + offset.to_bytes(16, 'little')).digest(size)


def evict(fd, start=0, length=0):
    if hasattr(os, 'posix_fadvise'):
        try:
            os.posix_fadvise(fd, start, length, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass


def write_all(fd, data):
    total = 0
    while total < len(data):
        try:
            n = os.write(fd, data[total:])
        except OSError as exc:
            exc.bytes_written = total
            raise
        if n == 0:
            exc = OSError(errno.EIO, 'zero-length write')
            exc.bytes_written = total
            raise exc
        total += n
    return total


class Session:
    def __init__(self, target, block_size=BLOCK, file_size=256*MIB, progress=print):
        self.target = Path(target)
        self.block_size = block_size
        self.file_size = file_size
        self.progress = progress
        self.directory = Path(tempfile.mkdtemp(prefix='.truecap-', dir=str(target)))
        self.seed = os.urandom(32)
        self.files = []
        self.written = 0
        self.write_error = None
        self.reached_full = False
        self.direct_reads = 0
        self.buffered_reads = 0

    def fill(self, limit=None):
        """Fill until ENOSPC, or stop at an explicit bounded test limit."""
        stop = False
        while not stop:
            path = self.directory / ('part-%06d.bin' % len(self.files))
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    self.reached_full = True
                else:
                    self.write_error = str(exc)
                break
            extents = []
            self.files.append((path, extents))
            local = 0
            try:
                while local < self.file_size:
                    n = min(self.block_size, self.file_size - local)
                    if limit is not None:
                        n = min(n, limit - self.written)
                    if n <= 0:
                        stop = True
                        break
                    start = self.written
                    data = pattern(self.seed, start, n)
                    try:
                        wrote = write_all(fd, data)
                    except OSError as exc:
                        wrote = getattr(exc, 'bytes_written', 0)
                        if exc.errno == errno.ENOSPC:
                            self.reached_full = True
                        else:
                            self.write_error = str(exc)
                        stop = True
                    if wrote:
                        extents.append((local, start, wrote))
                        self.written += wrote
                        local += wrote
                    if stop:
                        break
                os.fsync(fd)
                evict(fd)
            except OSError as exc:
                self.write_error = str(exc)
                stop = True
            finally:
                os.close(fd)
            self.progress('Written %s bytes; synced file %s' % (self.written, path.name))
            if limit is not None and self.written >= limit:
                break

    def quick(self):
        """Sample offsets in ONE bounded 64 MiB logical test file, not entire media."""
        path = self.directory / 'quick.bin'
        extents = []
        self.files.append((path, extents))
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            for offset in (0, 8*MIB, 16*MIB, 32*MIB, 63*MIB):
                os.lseek(fd, offset, os.SEEK_SET)
                data = pattern(self.seed, offset, BLOCK)
                n = write_all(fd, data)
                extents.append((offset, offset, n))
                self.written += n
            os.fsync(fd)
            evict(fd)
        except OSError as exc:
            self.write_error = str(exc)
        finally:
            os.close(fd)
        self.progress('Quick samples written: %s bytes across 64 MiB logical file.' % self.written)

    def _read(self, buffered, direct, offset, n):
        if direct is not None and offset % 4096 == 0 and n % 4096 == 0:
            try:
                with mmap.mmap(-1, n) as aligned:
                    os.lseek(direct, offset, os.SEEK_SET)
                    got = os.readv(direct, [aligned])
                    data = aligned[:got]
                self.direct_reads += 1
                return data
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS):
                    raise
        evict(buffered, offset, n)
        os.lseek(buffered, offset, os.SEEK_SET)
        pieces = []
        remaining = n
        while remaining:
            data = os.read(buffered, remaining)
            if not data:
                break
            pieces.append(data)
            remaining -= len(data)
        self.buffered_reads += 1
        return b''.join(pieces)

    def verify(self):
        good = bad = 0
        first = None
        errors = []
        error_count = 0
        for path, extents in self.files:
            fd = os.open(str(path), os.O_RDONLY)
            direct = None
            if hasattr(os, 'O_DIRECT') and hasattr(os, 'readv'):
                try:
                    direct = os.open(str(path), os.O_RDONLY | os.O_DIRECT)
                except OSError:
                    pass
            try:
                for local, absolute, n in extents:
                    expected = pattern(self.seed, absolute, n)
                    try:
                        data = self._read(fd, direct, local, n)
                    except OSError as exc:
                        data = b''
                        error_count += 1
                        if len(errors) < 10:
                            errors.append(str(exc))
                    if data == expected:
                        good += n
                    else:
                        bad += n
                        if first is None:
                            mismatch = next((i for i, (a, b) in enumerate(zip(data, expected)) if a != b), min(len(data), n))
                            first = {'file': path.name, 'file_offset': local + mismatch,
                                     'test_offset': absolute + mismatch}
            finally:
                os.close(fd)
                if direct is not None:
                    os.close(direct)
            self.progress('Checked %s: %s bytes in fully matching blocks so far.' % (path.name, good))
        return {'written_bytes': self.written, 'verified_bytes': good,
                'failed_block_bytes': bad, 'first_corruption': first,
                'reached_full': self.reached_full, 'write_error': self.write_error,
                'read_errors': errors, 'read_error_count': error_count, 'direct_read_blocks': self.direct_reads,
                'buffered_read_blocks': self.buffered_reads,
                'interpretation': 'Verified bytes are test data, NOT an exact physical capacity.'}

    def cleanup(self):
        # Never recurse or scan/delete unrelated files. Only exact session-created paths.
        for path, _ in self.files:
            path.unlink(missing_ok=True)
        self.directory.rmdir()


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return ''


def unescape_mount(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), value)


def mounts_from(path):
    mounts = []
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if len(fields) < 7 or '-' not in fields:
            continue
        split = fields.index('-')
        if len(fields) <= split + 2:
            continue
        mounts.append({'dev': fields[2], 'path': unescape_mount(fields[4]),
                       'source': unescape_mount(fields[split + 2])})
    return mounts


def decode_label(value):
    # /dev/disk/by-label names use udev hex escapes, e.g. My\x20USB.
    return re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), value)


def drive_list(sysroot='/sys', mountinfo='/proc/self/mountinfo', devroot='/dev'):
    """Read sysfs topology and mountinfo. Exclude whole root/boot backing disks."""
    sysroot = Path(sysroot)
    devroot = Path(devroot)
    blocks = sysroot / 'class/block'
    nodes = {}
    for entry in blocks.iterdir():
        resolved = entry.resolve()
        dev = read_text(entry / 'dev')
        if not dev:
            continue
        nodes[entry.name] = {'name': entry.name, 'path': resolved, 'dev': dev,
                             'partition': (entry / 'partition').exists()}
    mounts = mounts_from(mountinfo)
    by_dev = {node['dev']: name for name, node in nodes.items()}

    def disk_for(name):
        if nodes[name]['partition']:
            parent = nodes[name]['path'].parent.name
            if parent not in nodes:
                raise ValueError('Cannot resolve partition parent for ' + name)
            return parent
        return name

    def backing(name, seen=None):
        seen = set() if seen is None else seen
        if name in seen:
            return set()
        seen.add(name)
        disk = disk_for(name)
        result = {disk}
        slaves = blocks / disk / 'slaves'
        if slaves.is_dir():
            for slave in slaves.iterdir():
                if slave.name not in nodes:
                    raise ValueError('Cannot resolve system backing device')
                result.update(backing(slave.name, seen))
        return result

    protected = set()
    root_found = False
    for mount in mounts:
        if mount['path'] == '/' or mount['path'] == '/boot' or mount['path'].startswith('/boot/'):
            name = by_dev.get(mount['dev'])
            if name is None:
                # A missing root mapping means hiding the main disk cannot be guaranteed.
                if mount['path'] == '/':
                    raise ValueError('Cannot resolve main drive from mountinfo/sysfs; refusing to list targets.')
                raise ValueError('Cannot resolve boot backing device; refusing to list targets.')
            protected.update(backing(name))
            root_found = root_found or mount['path'] == '/'
    if not root_found:
        raise ValueError('No root mount identity found; refusing to list targets.')
    labels = {}
    labeldir = devroot / 'disk/by-label'
    if labeldir.is_dir():
        for label in labeldir.iterdir():
            if label.is_symlink():
                labels.setdefault(label.resolve().name, []).append(decode_label(label.name))
    rows = []
    for name, node in sorted(nodes.items()):
        disk = disk_for(name)
        if disk in protected:
            continue
        diskpath = nodes[disk]['path']
        ports = [p.name for p in (diskpath, *diskpath.parents)
                 if re.fullmatch(r'\d+-\d+(?:\.\d+)*', p.name)]
        usb = bool(ports)
        removable = read_text(blocks / disk / 'removable') == '1'
        if not (usb or removable):
            continue
        # When partitions exist, show volumes rather than a redundant unmounted parent.
        if not node['partition'] and any(n['partition'] and disk_for(k) == disk for k, n in nodes.items()):
            continue
        model = ' '.join(filter(None, (read_text(blocks / disk / 'device/vendor'),
                                        read_text(blocks / disk / 'device/model'))))
        paths = sorted({m['path'] for m in mounts if m['dev'] == node['dev']})
        rows.append({'device': str(devroot / name), 'dev': node['dev'],
                     'mount_paths': paths, 'label': ', '.join(sorted(labels.get(name, []))) or '(unlabelled)',
                     'name': model or disk, 'usb_port': ports[0] if ports else '(not USB / unknown)',
                     'removable': removable, 'usb': usb})
    return rows


def print_drives(rows):
    print('USB/removable volumes (main/root/boot backing disks hidden):')
    if not rows:
        print('No eligible USB/removable volumes found. Nothing written.')
    for row in rows:
        print('Device: %s | Name: %s | Label: %s' % (row['device'], row['name'], row['label']))
        print('  Mount path: %s | USB bus/port topology: %s' %
              (', '.join(row['mount_paths']) or '(not mounted)', row['usb_port']))
    print('Listing is read-only. USB port means bus/port topology, not a labelled socket on the case.')


def validate_target(value):
    path = Path(value).resolve(strict=True)
    if not path.is_dir() or not os.path.ismount(str(path)):
        raise ValueError('Use the root of a mounted USB filesystem, not a device or subfolder.')
    if path.stat().st_dev == Path('/').stat().st_dev:
        raise ValueError('Refusing the system/root filesystem.')
    if path in (Path('/boot'), Path('/boot/firmware')):
        raise ValueError('Refusing the boot filesystem.')
    entries = [p.name for p in path.iterdir() if p.name != 'lost+found']
    if entries:
        raise ValueError('Drive must be empty. Back up and remove existing files yourself first. Found: ' + ', '.join(entries[:5]))
    lost = path / 'lost+found'
    if lost.exists() and (lost.is_symlink() or not lost.is_dir() or any(lost.iterdir())):
        raise ValueError('lost+found must be an empty, ordinary directory.')
    rows = drive_list()
    dev = '%s:%s' % (os.major(path.stat().st_dev), os.minor(path.stat().st_dev))
    if not any(row['dev'] == dev and str(path) in row['mount_paths'] for row in rows):
        raise ValueError('Target is not an eligible USB/removable volume, or is on the main/root/boot drive.')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', nargs='?', help='root of an empty mounted disposable USB filesystem')
    parser.add_argument('--list', action='store_true', help='list USB/removable volumes without writing')
    parser.add_argument('--yes', action='store_true', help='accept ALL data-loss warnings without typing yes')
    parser.add_argument('--quick', action='store_true', help='5 samples in a bounded 64 MiB file; no capacity proof')
    parser.add_argument('--limit-mib', type=int, help='bounded full test; not a whole-drive capacity test')
    parser.add_argument('--file-mib', type=int, default=256, help='file chunk size, 1 to 1024 MiB (default 256)')
    args = parser.parse_args(argv)
    if args.list or args.path is None:
        try:
            print_drives(drive_list())
            return 0
        except (OSError, ValueError) as exc:
            print('Cannot list drives safely: %s' % exc, file=sys.stderr)
            return 3
    if not 1 <= args.file_mib <= 1024 or (args.limit_mib is not None and args.limit_mib < 1):
        parser.error('Sizes must be positive; --file-mib must be 1 to 1024.')
    if args.quick and args.limit_mib is not None:
        parser.error('--quick and --limit-mib cannot be combined.')
    try:
        target = validate_target(args.path)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    try:
        row = next((r for r in drive_list() if str(target) in r['mount_paths']), None)
        if row is not None:
            print_drives([row])
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    usage = shutil.disk_usage(target)
    print('Target: %s\nFilesystem reports %s total bytes, %s free bytes.' % (target, usage.total, usage.free))
    print('WARNING: Use only an empty, expendable USB. Back up ALL data first.')
    print('A fake drive can overwrite existing data/metadata. Even this test can break its filesystem.')
    print('Full mode fills available space, writes then reads everything, takes hours on large/slow drives, and wears flash.')
    print('Quick mode samples only 64 MiB logical space. Passing it does NOT establish capacity.')
    print('Do not unplug, mount elsewhere, or run other writers during the test. Check the target carefully.')
    if not args.yes:
        try:
            if input('Type yes to accept the data-loss risk and start: ').strip() != 'yes':
                print('Cancelled. Nothing written.')
                return 2
        except EOFError:
            print('Cancelled. Nothing written.')
            return 2
    session = None
    result = None
    status = 3
    try:
        target = validate_target(target)
        session = Session(target, file_size=args.file_mib*MIB)
        print('Session directory: %s' % session.directory)
        if args.quick:
            session.quick()
        else:
            session.fill(None if args.limit_mib is None else args.limit_mib*MIB)
        print('Reading back after synced writes. Direct reads bypass OS page cache where supported.')
        result = session.verify()
        result['mode'] = 'quick' if args.quick else ('bounded' if args.limit_mib else 'full')
        result['filesystem_reported_total_bytes'] = usage.total
        result['filesystem_initial_free_bytes'] = usage.free
        if result['failed_block_bytes'] or result['write_error'] or result['read_errors']:
            status = 1
            result['verdict'] = 'Corruption or I/O failure detected. Fake capacity is one possible cause, not the only one.'
        elif not session.written:
            status = 3
            result['verdict'] = 'No data tested. Inconclusive.'
        else:
            status = 0
            result['verdict'] = 'Written test blocks matched. This does not establish exact physical capacity or long-term reliability.'
        print(json.dumps(result, indent=2))
        print('Verified %s bytes (%.3f GiB) in fully matching blocks. NOT exact physical capacity.' % (result['verified_bytes'], result['verified_bytes'] / (1024**3)))
        if result['buffered_read_blocks']:
            print('Some reads were buffered. Cache eviction is best effort; a pass may be less conclusive.')
        if result['first_corruption']:
            print('First bad test offset is a logical test offset, NOT a reliable physical-size estimate.')
    except KeyboardInterrupt:
        print('\nInterrupted. Cleaning only this session\'s test files.')
        status = 130
    except (OSError, ValueError) as exc:
        print('Test stopped: %s' % exc, file=sys.stderr)
        status = 3
    finally:
        if session is not None:
            try:
                session.cleanup()
                print('Session files removed.')
            except OSError as exc:
                print('Cleanup incomplete: %s. Session directory: %s' % (exc, session.directory), file=sys.stderr)
                status = 3
    return status


if __name__ == '__main__':
    sys.exit(main())
