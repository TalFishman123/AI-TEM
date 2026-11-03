import concurrent.futures
import importlib.util
import os
from pathlib import Path
import runpy
import socket
import json
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
ROOT = BASE / 'utem_logger'
HELPER = BASE / 'dm_helper' / 'utem_view_server.py'
from utem_logger import config, logger
from utem_logger.pyjem_source import PyJEMSource, Unavailable


class Repairs(unittest.TestCase):
    def test_timeout_does_not_overlap_or_reconnect_and_discards_late_state(self):
        cfg = dict(config.DEFAULTS['pyjem'], getters=['Lens3.GetCL3'], fast=[], timeout_s=0.01)
        source = PyJEMSource(cfg)
        gate = threading.Event()
        entered = threading.Event()
        def getter():
            entered.set()
            if not gate.wait(2):
                raise AssertionError('test did not release getter')
            return 123
        source._connect = Mock(return_value={'Lens3': types.SimpleNamespace(GetCL3=getter)})
        try:
            with self.assertRaises(Unavailable):
                source.read()
            self.assertTrue(entered.is_set())
            future, pool = source.future, source.pool
            for _ in range(4):
                with self.assertRaisesRegex(Unavailable, 'still running'):
                    source.read()
            self.assertIs(source.future, future)
            self.assertIs(source.pool, pool)
            self.assertEqual(source._connect.call_count, 1)
            gate.set()
            future.result(timeout=2)
            with self.assertRaisesRegex(Unavailable, 'late PyJEM result discarded'):
                source.read()
            self.assertEqual(source.read()[0], {'Lens3.GetCL3': 123})
            self.assertEqual(source._connect.call_count, 1)
        finally:
            gate.set()
            source.close()

    def test_worker_exception_allows_sequential_reconnect(self):
        source = PyJEMSource(config.DEFAULTS['pyjem'])
        source._read = Mock(side_effect=[RuntimeError('connection lost'), ({'ok': 1}, {})])
        try:
            with self.assertRaisesRegex(Unavailable, 'connection lost'):
                source.read()
            self.assertEqual(source.read(), ({'ok': 1}, {}))
        finally:
            source.close()
        with self.assertRaisesRegex(Unavailable, 'closed'):
            source.read()

    def test_retry_starts_after_failed_read(self):
        obj = logger.Logger.__new__(logger.Logger)
        obj.available, obj.next_pyjem_try = True, 0
        obj.cfg = config.DEFAULTS
        obj.source = object()
        obj.status = Mock()
        with patch.object(logger.time, 'monotonic', side_effect=[10, 100]), patch.object(
                logger, 'timed_read', side_effect=Unavailable('slow')):
            obj.poll()
        self.assertEqual(obj.next_pyjem_try, 130)

    def test_quiet_dm_start_defers_every_dm_call_until_request(self):
        with patch.dict(os.environ, {'UTEM_VIEW_SERVER_NO_AUTOSTART': '1',
                                    'UTEM_VIEW_SERVER_QUIET_START': '1'}):
            ns = runpy.run_path(str(HELPER))
        g = ns['main'].__globals__
        dm = Mock()
        g['DM'] = dm
        g['serve'] = Mock()
        g['stop_previous_run'] = Mock()
        g['gms_version'] = Mock(return_value=(3, 4, 4))
        g['find_view'] = Mock(return_value=None)
        g['main']()
        self.assertEqual(dm.mock_calls, [])
        g['gms_version'].assert_not_called()
        g['find_view'].assert_not_called()
        g['answer']('probe')
        g['gms_version'].assert_called_once()
        g['find_view'].assert_called_once()
        g['answer']('probe')
        g['gms_version'].assert_called_once()

    def test_background_logging_never_calls_dm(self):
        with patch.dict(os.environ, {'UTEM_VIEW_SERVER_NO_AUTOSTART': '1'}):
            ns = runpy.run_path(str(HELPER))
        g = ns['_log'].__globals__
        g['DM'] = Mock()
        g['_dm_thread_ident'] = threading.get_ident()
        t = threading.Thread(target=g['_log'], args=('test',))
        t.start()
        t.join(2)
        g['DM'].Result.assert_not_called()

    def test_quiet_server_has_no_self_test_and_answers_real_socket(self):
        with patch.dict(os.environ, {'UTEM_VIEW_SERVER_NO_AUTOSTART': '1',
                                    'UTEM_VIEW_SERVER_QUIET_START': '1'}):
            ns = runpy.run_path(str(HELPER))
        g = ns['serve'].__globals__
        g['PORT'] = 0
        ready = threading.Event()
        g['_log'] = lambda msg: ready.set() if msg.startswith('listening') else None
        g['gms_version'] = Mock(return_value=(3, 4, 4))
        g['find_view'] = Mock(return_value=None)
        with patch.object(threading, 'Timer') as timer:
            t = threading.Thread(target=g['serve'])
            t.start()
            try:
                self.assertTrue(ready.wait(2))
                timer.assert_not_called()
                g['find_view'].assert_not_called()
                port = g['_run']['socket'].getsockname()[1]
                with socket.create_connection(('127.0.0.1', port), timeout=2) as c:
                    c.sendall(b'{"cmd":"status"}\n')
                    response = json.loads(c.makefile('rb').readline())
                self.assertEqual(response['version'], '0.4.2')
                self.assertEqual(response['gms'], [3, 4, 4])
                self.assertFalse(response['live'])
            finally:
                g['_run']['stop'] = True
                g['_run']['socket'].close()
                t.join(3)
            self.assertFalse(t.is_alive())

    def test_configuration(self):
        cfg = config.load(BASE / 'config.example.json')
        self.assertEqual(cfg['poll_interval_s'], 30)
        self.assertEqual(config.DEFAULTS['poll_interval_s'], 30)
        self.assertNotIn('VACUUM3.GetSpecimenPreEvacStart', cfg['pyjem']['getters'])


if __name__ == '__main__':
    unittest.main()
