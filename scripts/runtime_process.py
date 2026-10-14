"""Unprivileged Linux supervision for one bounded, privately logged process tree.

The dedicated child is a process-local subreaper. Only its descendants are
signalled, using PID file descriptors plus start-time identity checks; sessions
and GNU timeout process groups cannot escape ownership. No command-line API.
"""
import ctypes
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time


class ProcessInterrupted(BaseException):
    pass


def require_pidfd_capability():
    """Probe the real syscalls; exposed Python names do not prove kernel access."""
    descriptor = None
    try:
        descriptor = os.pidfd_open(os.getpid())
        signal.pidfd_send_signal(descriptor, 0)
    except OSError:
        raise RuntimeError('Required pidfd supervision capability is unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def process_info(pid):
    try:
        fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[1]), fields[19], fields[0]
    except (FileNotFoundError, ProcessLookupError):
        return None


def descendants(root):
    if process_info(root) is None:
        raise RuntimeError('Supervisor identity disappeared')
    entries = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        try:
            value = process_info(int(path.name))
        except PermissionError:
            continue
        if value is not None:
            entries[int(path.name)] = value
    owned = {root}
    while True:
        additions = {pid for pid, (parent, _, _) in entries.items() if parent in owned} - owned
        if not additions:
            return {pid: entries[pid] for pid in owned - {root}}
        owned.update(additions)


def signal_exact(pid, start_time, signum):
    """A recycled numeric PID is never permission to signal another process."""
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        observed = process_info(pid)
        if observed is not None and observed[1] == start_time:
            signal.pidfd_send_signal(descriptor, signum)
    except ProcessLookupError:
        pass
    finally:
        os.close(descriptor)


def reap_children():
    while True:
        try:
            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                return
        except ChildProcessError:
            return


def terminate_descendants(root):
    """Terminate and reap all owned descendants, including newly adopted orphans."""
    deadline = time.monotonic() + 4
    escalation = time.monotonic() + .25
    while time.monotonic() < deadline:
        reap_children()
        owned = descendants(root)
        if not owned:
            return True
        for pid, (_, start, state) in owned.items():
            if state != 'Z':
                try:
                    signal_exact(pid, start, signal.SIGTERM if time.monotonic() < escalation else signal.SIGKILL)
                except OSError:
                    # Keep ownership and attempt the other exact descendants.
                    # A transient syscall interruption can recover on the next
                    # sweep; a persistent denial remains unverified, with no
                    # alternate signalling route around the denied capability.
                    continue
        time.sleep(.02)
    reap_children()
    return not descendants(root)


def supervise(command, output_fd, timeout, env, cwd, limit_bytes, expected_parent):
    result = {'exit_status': 127, 'timed_out': False, 'output_limit_exceeded': False,
        'interrupted': False, 'descendant_cleanup': False, 'orphaned_descendants': False}
    child = None
    def interrupted(*_):
        raise ProcessInterrupted()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        require_pidfd_capability()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER, this process only
            raise OSError('Process-local subreaper is unavailable')
        # The caller relays termination. A killed caller also asks this exact
        # supervisor to clean its tree, without changing any system setting.
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != expected_parent:
            raise OSError('Parent lifetime supervision is unavailable')
        admitted_subreaper = ctypes.c_int()
        admitted_parent_signal = ctypes.c_int()
        if (libc.prctl(37, ctypes.byref(admitted_subreaper), 0, 0, 0) != 0
                or libc.prctl(2, ctypes.byref(admitted_parent_signal), 0, 0, 0) != 0
                or admitted_subreaper.value != 1 or admitted_parent_signal.value != signal.SIGTERM):
            raise OSError('Process-local supervision could not be verified')
        if descendants(os.getpid()):
            raise RuntimeError('Supervisor must begin with an empty owned tree')
        with os.fdopen(output_fd, 'wb', closefd=False) as output, selectors.DefaultSelector() as selector:
            child = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            selector.register(child.stdout, selectors.EVENT_READ)
            count = 0
            deadline = time.monotonic() + timeout
            while selector.get_map():
                if time.monotonic() >= deadline:
                    result.update(exit_status=124, timed_out=True)
                    break
                for key, _ in selector.select(min(.05, max(0, deadline-time.monotonic()))):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    allowed = min(len(data), max(0, limit_bytes-count))
                    output.write(data[:allowed]); output.flush(); count += len(data)
                    if count > limit_bytes:
                        result.update(exit_status=125, output_limit_exceeded=True)
                        break
                if result['output_limit_exceeded']:
                    break
            if not result['timed_out'] and not result['output_limit_exceeded']:
                try:
                    result['exit_status'] = child.wait(timeout=max(.001, deadline-time.monotonic()))
                except subprocess.TimeoutExpired:
                    result.update(exit_status=124, timed_out=True)
    except ProcessInterrupted:
        result.update(exit_status=143, interrupted=True)
    except Exception:
        result['exit_status'] = 127
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            if child is not None:
                child.poll()  # Preserve direct status before adoption reaping.
                if result['exit_status'] == 0 and descendants(os.getpid()):
                    result.update(exit_status=126, orphaned_descendants=True)
                result['descendant_cleanup'] = terminate_descendants(os.getpid())
                child.stdout.close()
            else:
                result['descendant_cleanup'] = not descendants(os.getpid())
        except Exception:
            # Never let a cleanup exception silently erase the control result.
            # A bounded retry retains the same exact descendant ownership.
            try:
                result['descendant_cleanup'] = terminate_descendants(os.getpid())
            except Exception:
                result['descendant_cleanup'] = False
            result['exit_status'] = 126
        if not result['descendant_cleanup']:
            result['exit_status'] = 126
    return result


def bounded_process(command, output, *, timeout, env=None, limit_bytes=16*1024*1024, cwd=None):
    """Return a bounded result only after the dedicated supervisor has reaped work."""
    if sys.platform != 'linux' or not all(hasattr(os, name) for name in ('fork', 'pidfd_open')) or not hasattr(signal, 'pidfd_send_signal'):
        raise RuntimeError('Required unprivileged Linux process supervision is unavailable')
    require_pidfd_capability()
    if timeout <= 0 or type(limit_bytes) is not int or limit_bytes <= 0:
        raise ValueError('Positive fixed process bounds are required')
    output_fd = os.open(output, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    read_fd, write_fd = os.pipe()
    expected_parent = os.getpid()
    supervisor = os.fork()
    if supervisor == 0:
        os.close(read_fd)
        try:
            os.setsid()
            # An inherited work alarm belongs only to the caller.
            signal.setitimer(signal.ITIMER_REAL, 0)
            result = supervise(command, output_fd, timeout, env, cwd, limit_bytes, expected_parent)
            os.write(write_fd, json.dumps(result).encode())
        except BaseException:
            # Even an unexpected control/serialization failure must finish an
            # exact cleanup attempt before this ownership boundary exits.
            try:
                terminate_descendants(os.getpid())
            except BaseException:
                pass
        finally:
            os.close(output_fd); os.close(write_fd)
        os._exit(0)
    os.close(output_fd); os.close(write_fd)
    start = process_info(supervisor)
    handlers = {}
    def relay(signum, frame):
        if start is not None:
            signal_exact(supervisor, start[1], signal.SIGTERM)
        previous = handlers[signum]
        if callable(previous):
            previous(signum, frame)
        raise SystemExit(128+signum)
    for signum in (signal.SIGTERM, signal.SIGINT):
        handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, relay)
    data = b''
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(read_fd, selectors.EVENT_READ)
            deadline = time.monotonic()+timeout+8
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise RuntimeError('Process supervisor did not finish within its reserve')
                for _, _ in selector.select(.05):
                    chunk = os.read(read_fd, 4096)
                    if not chunk:
                        selector.unregister(read_fd)
                    else:
                        data += chunk
                        if len(data) > 4096:
                            raise RuntimeError('Unbounded supervisor result')
        os.waitpid(supervisor, 0)
        try:
            result = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError('Invalid or missing supervisor result') from None
        keys = {'exit_status','timed_out','output_limit_exceeded','interrupted','descendant_cleanup','orphaned_descendants'}
        if (type(result) is not dict or set(result) != keys or type(result['exit_status']) is not int
                or any(type(result[key]) is not bool for key in keys - {'exit_status'})):
            raise RuntimeError('Invalid supervisor result')
        return result
    finally:
        for signum, previous in handlers.items():
            signal.signal(signum, previous)
        os.close(read_fd)
        # This also covers parent alarms/exceptions and termination during a read.
        if process_info(supervisor) is not None:
            if start is not None:
                signal_exact(supervisor, start[1], signal.SIGTERM)
            end = time.monotonic()+5
            while time.monotonic() < end:
                try:
                    if os.waitpid(supervisor, os.WNOHANG)[0]:
                        break
                except ChildProcessError:
                    break
                time.sleep(.02)
            else:
                # Fail closed; never declare successful tree cleanup from merely
                # killing the supervisor. Its child-lifetime handler normally
                # completes the exact descendant sweep before this bound.
                raise RuntimeError('Owned supervisor cleanup could not be verified')
