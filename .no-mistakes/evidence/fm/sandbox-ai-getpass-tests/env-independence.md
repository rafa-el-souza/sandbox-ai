# Evidence: workspace-bridge checks are env-independent

The change adds a module-level autouse fixture that pins
`core.doctor.checks.workspace_bridge.getpass.getuser` to a fixed name, so the two
env-dependent tests no longer pass/fail based on whether the runner exports
`LOGNAME`/`USER`/`LNAME`/`USERNAME`.

All runs below are performed with **those four env vars explicitly unset** -
reproducing the no-mistakes gate container, the exact environment the intent
targets.

```
$ python3 -c 'import os;print({k:os.environ.get(k) for k in ("LOGNAME","USER","LNAME","USERNAME")})'
{'LOGNAME': None, 'USER': None, 'LNAME': None, 'USERNAME': None}
```

## Before the fix (fixture removed) - tests fail by environment

With the autouse fixture removed and env vars unset, `getpass.getuser()` inside
`_load_host_settings_or_skip` falls through to `pwd.getpwuid(os.getuid())`, which
the tests have replaced with a fake `_Pw` record, raising `TypeError` before the
assert ever runs:

```
src/core/doctor/checks/workspace_bridge.py:47: in _load_host_settings_or_skip
    return HostConfig.from_marker(getpass.getuser()).host
...
>           return pwd.getpwuid(os.getuid())[0]
E           TypeError: '_Pw' object is not subscriptable

FAILED ...::TestCheckDevInWorkspaceBridgeGroup::test_fail_relogin_path
FAILED ...::TestCheckDevInWorkspaceBridgeGroup::test_fail_usermod_path
2 failed in 0.37s
```

## After the fix (fixture present, as shipped) - tests pass deterministically

```
tests/unit/core/doctor/checks/test_workspace_bridge.py::TestCheckDevInWorkspaceBridgeGroup::test_fail_relogin_path PASSED [ 50%]
tests/unit/core/doctor/checks/test_workspace_bridge.py::TestCheckDevInWorkspaceBridgeGroup::test_fail_usermod_path PASSED [100%]
2 passed in 0.74s
```

Full test file, still with env vars unset:

```
62 passed in 0.40s
```

The pinned `getpass.getuser` returns `"operator"` regardless of the runner's
environment, so operator resolution no longer reaches the fake `pwd` record.
