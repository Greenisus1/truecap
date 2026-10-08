#!/usr/bin/env python3
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import truecap


class TruecapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = None

    def tearDown(self):
        if self.session is not None and self.session.directory.exists():
            self.session.cleanup()
        self.temp.cleanup()

    def session_at(self, **kwargs):
        self.session = truecap.Session(self.root, progress=lambda _: None, **kwargs)
        return self.session

    def test_pattern_deterministic_distinct_and_prefix(self):
        seed = b'x' * 32
        self.assertEqual(truecap.pattern(seed, 0, 100), truecap.pattern(seed, 0, 100))
        self.assertNotEqual(truecap.pattern(seed, 0, 100), truecap.pattern(seed, 1, 100))
        self.assertNotEqual(truecap.pattern(seed, 0, 100), truecap.pattern(b'y'*32, 0, 100))
        self.assertEqual(truecap.pattern(seed, 0, 100)[:31], truecap.pattern(seed, 0, 31))

    def test_full_logic_small_real_files(self):
        s = self.session_at(block_size=65536, file_size=262144)
        s.fill(limit=2*truecap.MIB+123)
        r = s.verify()
        self.assertEqual(r['verified_bytes'], 2*truecap.MIB+123)
        self.assertEqual(r['failed_block_bytes'], 0)
        self.assertIsNone(r['first_corruption'])
        self.assertFalse(r['reached_full'])
        self.assertEqual(len(s.files), 9)

    def test_corruption_exact_first_byte_and_block_count(self):
        s = self.session_at(block_size=4096, file_size=8192)
        s.fill(limit=16384)
        with s.files[0][0].open('r+b') as f:
            f.seek(4103)
            byte = f.read(1)
            f.seek(4103)
            f.write(bytes([byte[0] ^ 255]))
            f.flush()
            os.fsync(f.fileno())
        r = s.verify()
        self.assertEqual(r['verified_bytes'], 12288)
        self.assertEqual(r['failed_block_bytes'], 4096)
        self.assertEqual(r['first_corruption']['test_offset'], 4103)

    def test_truncation_detected(self):
        s = self.session_at(block_size=4096, file_size=8192)
        s.fill(limit=8192)
        os.truncate(s.files[0][0], 4096)
        r = s.verify()
        self.assertEqual(r['verified_bytes'], 4096)
        self.assertEqual(r['failed_block_bytes'], 4096)
        self.assertEqual(r['first_corruption']['test_offset'], 4096)

    def test_fake_wrap_simulation_overwrites_earlier_pattern(self):
        s = self.session_at(block_size=4096, file_size=4096)
        s.fill(limit=16384)
        later = s.files[-1][0].read_bytes()
        s.files[0][0].write_bytes(later)
        r = s.verify()
        self.assertEqual(r['first_corruption']['test_offset'], 0)
        self.assertEqual(r['verified_bytes'], 12288)
        # First corruption at 0 demonstrably is NOT physical capacity.

    def test_fill_to_simulated_enospc_partial_write(self):
        s = self.session_at(block_size=4096, file_size=8192)
        real_write = os.write
        budget = [10001]
        def limited_write(fd, data):
            if budget[0] == 0:
                raise OSError(errno.ENOSPC, 'simulated device full')
            n = real_write(fd, data[:budget[0]])
            budget[0] -= n
            return n
        with patch('truecap.os.write', limited_write):
            s.fill()
        r = s.verify()
        self.assertTrue(r['reached_full'])
        self.assertEqual(r['written_bytes'], 10001)
        self.assertEqual(r['verified_bytes'], 10001)
        self.assertIsNone(r['write_error'])

    def test_write_io_error_not_reported_as_full(self):
        s = self.session_at(block_size=4096, file_size=8192)
        with patch('truecap.os.write', side_effect=OSError(errno.EIO, 'simulated write error')):
            s.fill()
        self.assertFalse(s.reached_full)
        self.assertIn('simulated write error', s.write_error)
        self.assertEqual(s.verify()['verified_bytes'], 0)

    def test_read_io_error(self):
        s = self.session_at(block_size=4096, file_size=8192)
        s.fill(limit=8192)
        with patch.object(s, '_read', side_effect=OSError(errno.EIO, 'simulated read error')):
            r = s.verify()
        self.assertEqual(r['verified_bytes'], 0)
        self.assertEqual(r['failed_block_bytes'], 8192)
        self.assertEqual(len(r['read_errors']), 2)

    def test_cleanup_never_removes_unrelated_files(self):
        sentinel = self.root / 'keep.txt'
        sentinel.write_text('precious')
        s = self.session_at(block_size=4096, file_size=8192)
        s.fill(limit=8192)
        directory = s.directory
        s.cleanup()
        self.assertFalse(directory.exists())
        self.assertEqual(sentinel.read_text(), 'precious')
        self.assertEqual(list(self.root.iterdir()), [sentinel])

    def test_cleanup_refuses_recursive_delete(self):
        s = self.session_at(block_size=4096, file_size=8192)
        s.fill(limit=8192)
        outsider = s.directory / 'unrelated.txt'
        outsider.write_text('keep')
        with self.assertRaises(OSError):
            s.cleanup()
        self.assertEqual(outsider.read_text(), 'keep')
        outsider.unlink()

    def test_quick_mode_real_sparse_file(self):
        s = self.session_at()
        s.quick()
        r = s.verify()
        self.assertEqual(r['verified_bytes'], 5*truecap.MIB)
        self.assertEqual(s.files[0][0].stat().st_size, 64*truecap.MIB)
        self.assertFalse(r['reached_full'])

    def test_root_and_subdirectory_rejected(self):
        with self.assertRaises(ValueError):
            truecap.validate_target('/')
        with self.assertRaises(ValueError):
            truecap.validate_target(self.root)

    def test_nonempty_mounted_target_rejected(self):
        (self.root/'precious').write_text('keep')
        real_stat = Path.stat
        def fake_stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if path == self.root:
                values = list(st)
                values[2] = st.st_dev + 1
                return os.stat_result(values)
            return st
        with patch('truecap.os.path.ismount', return_value=True), patch.object(Path, 'stat', fake_stat):
            with self.assertRaisesRegex(ValueError, 'must be empty'):
                truecap.validate_target(self.root)

    def test_cancel_before_session_or_writes(self):
        with patch('truecap.validate_target', return_value=self.root), patch('builtins.input', return_value='no'):
            self.assertEqual(truecap.main([str(self.root)]), 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cli_bounded_result_and_cleanup(self):
        with patch('truecap.validate_target', return_value=self.root):
            self.assertEqual(truecap.main([str(self.root), '--yes', '--limit-mib', '1', '--file-mib', '1']), 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_interrupted_cli_cleans_created_files(self):
        original = truecap.Session.fill
        def interrupted(session, limit=None):
            original(session, limit=4096)
            raise KeyboardInterrupt
        with patch('truecap.validate_target', return_value=self.root), patch.object(truecap.Session, 'fill', interrupted):
            self.assertEqual(truecap.main([str(self.root), '--yes']), 130)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_fsync_failure_is_inconclusive(self):
        s = self.session_at(block_size=4096, file_size=8192)
        with patch('truecap.os.fsync', side_effect=OSError(errno.EIO, 'sync failed')):
            s.fill(limit=8192)
        self.assertIn('sync failed', s.write_error)


class DriveListingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.sys = self.base/'sys'
        self.blocks = self.sys/'class/block'
        self.blocks.mkdir(parents=True)
        self.dev = self.base/'dev'
        (self.dev/'disk/by-label').mkdir(parents=True)
        self.mountinfo = self.base/'mountinfo'
        self.add_disk('mmcblk0', '179:0', removable=True)
        self.add_partition('mmcblk0', 'mmcblk0p1', '179:1')
        self.add_partition('mmcblk0', 'mmcblk0p2', '179:2')
        self.add_disk('sda', '8:0', port='1-1.2', removable=False)
        self.add_partition('sda', 'sda1', '8:1')
        self.add_disk('nvme0n1', '259:0')
        self.mountinfo.write_text('1 0 179:2 / / rw - ext4 /dev/mmcblk0p2 rw\n2 1 179:1 / /boot rw - vfat /dev/mmcblk0p1 rw\n3 1 8:1 / /mnt/My\\040USB rw - vfat /dev/sda1 rw\n')
        (self.dev/'disk/by-label/My\\x20USB').symlink_to(self.dev/'sda1')

    def tearDown(self):
        self.temp.cleanup()

    def add_disk(self, name, dev, port=None, removable=False):
        where = self.sys/'devices'
        if port:
            where = where/'usb1'/'1-1'/port/'1-1.2:1.0'/'host0'
        real = where/'block'/name
        real.mkdir(parents=True)
        (real/'dev').write_text(dev)
        (real/'removable').write_text('1' if removable else '0')
        (real/'device').mkdir()
        (real/'device/vendor').write_text('Example')
        (real/'device/model').write_text('Flash Disk')
        (self.blocks/name).symlink_to(real)
        return real

    def add_partition(self, disk, name, dev):
        real = (self.blocks/disk).resolve()/name
        real.mkdir()
        (real/'dev').write_text(dev)
        (real/'partition').write_text('1')
        (self.blocks/name).symlink_to(real)

    def listing(self):
        return truecap.drive_list(self.sys, self.mountinfo, self.dev)

    def test_usb_port_model_label_mount_and_device(self):
        rows = self.listing()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['usb_port'], '1-1.2')
        self.assertEqual(rows[0]['mount_paths'], ['/mnt/My USB'])
        self.assertEqual(rows[0]['device'], str(self.dev/'sda1'))
        self.assertEqual(rows[0]['label'], 'My USB')
        self.assertEqual(rows[0]['name'], 'Example Flash Disk')
        self.assertFalse(rows[0]['removable'])
        self.assertTrue(rows[0]['usb'])

    def test_main_removable_card_and_internal_disk_hidden(self):
        self.assertEqual([r['dev'] for r in self.listing()], ['8:1'])

    def test_main_usb_disk_all_partitions_hidden(self):
        self.mountinfo.write_text('1 0 8:1 / / rw - ext4 /dev/sda1 rw\n')
        self.add_partition('sda', 'sda2', '8:2')
        self.assertFalse(any(r['dev'].startswith('8:') for r in self.listing()))

    def test_separate_boot_usb_disk_all_partitions_hidden(self):
        with self.mountinfo.open('a') as f:
            f.write('4 1 8:1 / /boot/firmware rw - vfat /dev/sda1 rw\n')
        self.assertEqual(self.listing(), [])

    def test_unmounted_usb_still_shown_with_unknown_label(self):
        self.add_disk('sdb', '8:16', port='2-3')
        row = next(r for r in self.listing() if r['dev']=='8:16')
        self.assertEqual(row['mount_paths'], [])
        self.assertEqual(row['label'], '(unlabelled)')
        self.assertEqual(row['usb_port'], '2-3')

    def test_device_mapper_main_backing_usb_hidden(self):
        real = self.add_disk('dm-0', '253:0')
        (real/'slaves').mkdir()
        (real/'slaves/sda1').symlink_to((self.blocks/'sda1').resolve())
        self.mountinfo.write_text('1 0 253:0 / / rw - ext4 /dev/mapper/main rw\n')
        self.assertFalse(any(r['dev'].startswith('8:') for r in self.listing()))

    def test_unresolved_main_identity_fails_closed(self):
        self.mountinfo.write_text('1 0 999:0 / / rw - ext4 /dev/unknown rw\n')
        with self.assertRaisesRegex(ValueError, 'Cannot resolve main'):
            self.listing()

    def test_no_path_or_list_is_read_only(self):
        with patch('truecap.drive_list', return_value=self.listing()), patch('truecap.Session') as s:
            self.assertEqual(truecap.main([]), 0)
            self.assertEqual(truecap.main(['--list']), 0)
            s.assert_not_called()

    def test_target_on_other_main_partition_refused(self):
        self.add_partition('mmcblk0', 'mmcblk0p3', '179:3')
        target = self.base/'empty-mount'
        target.mkdir()
        real_stat = Path.stat
        def fake_stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if path == target:
                vals = list(st)
                vals[2] = os.makedev(179, 3)
                return os.stat_result(vals)
            return st
        with patch('truecap.os.path.ismount', return_value=True), patch.object(Path, 'stat', fake_stat), patch('truecap.drive_list', return_value=self.listing()):
            with self.assertRaisesRegex(ValueError, 'not an eligible'):
                truecap.validate_target(target)


if __name__ == '__main__':
    unittest.main(verbosity=2)
