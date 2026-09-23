import unittest

from e10_mutex import (
    ERROR_ACCESS_DENIED,
    ERROR_ALREADY_EXISTS,
    MutexAlreadyExists,
    MutexCreationError,
    MutexDegradationNotAllowed,
    MutexNamespaceDenied,
    acquire_executor_mutex,
)


class FakeMutexApi:
    def __init__(self, results):
        self.results = list(results)
        self.created = []
        self.closed = []

    def create(self, name):
        self.created.append(name)
        return self.results.pop(0)

    def close(self, handle):
        self.closed.append(handle)


class ExecutorMutexTests(unittest.TestCase):
    def test_normal_path_holds_global_and_local(self):
        api = FakeMutexApi([("global", 0), ("local", 0)])
        lease = acquire_executor_mutex(api=api, lock_name="TEST")
        self.assertEqual(("Global", "Local"), lease.lock_namespaces)
        self.assertIsNone(lease.mutex_denied_reason)
        lease.release()
        self.assertEqual(["global", "local"], api.closed)

    def test_existing_global_executor_is_reported(self):
        api = FakeMutexApi([("existing", ERROR_ALREADY_EXISTS)])
        with self.assertRaisesRegex(
                MutexAlreadyExists, "已有另一个E10 RPA实例"):
            acquire_executor_mutex(api=api, lock_name="TEST")
        self.assertEqual(["existing"], api.closed)

    def test_global_access_denied_fails_closed_by_default(self):
        api = FakeMutexApi([(None, ERROR_ACCESS_DENIED)])
        with self.assertRaises(MutexNamespaceDenied) as caught:
            acquire_executor_mutex(
                api=api, risk="read_only", lock_name="TEST")
        self.assertEqual("MUTEX_NAMESPACE_DENIED",
                         caught.exception.failure_code)
        self.assertEqual(["Global\\TEST"], api.created)

    def test_explicit_readonly_degradation_uses_only_local_and_is_audited(self):
        api = FakeMutexApi([
            (None, ERROR_ACCESS_DENIED),
            ("local", 0),
        ])
        lease = acquire_executor_mutex(
            api=api, risk="read_only", lock_name="TEST",
            allow_readonly_degradation=True)
        self.assertEqual(("Local",), lease.lock_namespaces)
        self.assertIn("GLOBAL_ERROR_ACCESS_DENIED",
                      lease.mutex_denied_reason)
        lease.release()
        self.assertEqual(["local"], api.closed)

    def test_degradation_switch_is_rejected_before_commit_lock_attempt(self):
        api = FakeMutexApi([])
        with self.assertRaises(MutexDegradationNotAllowed):
            acquire_executor_mutex(
                api=api, risk="commit", lock_name="TEST",
                allow_readonly_degradation=True)
        self.assertEqual([], api.created)

    def test_unknown_winerror_is_not_guessed(self):
        api = FakeMutexApi([(None, 1234)])
        with self.assertRaises(MutexCreationError) as caught:
            acquire_executor_mutex(api=api, lock_name="TEST")
        self.assertEqual(1234, caught.exception.winerror)
        self.assertIn("WinError=1234", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
