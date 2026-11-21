#!/usr/bin/env python3
"""
The one-way tap from the logger to the live sidecar. Stdlib only.

log.py hands every record it has just archived to `Tap.send()`, which offers it to a Unix
datagram socket and forgets about it. live.py, a separate process under its own unit, binds
that socket, decodes what arrives, and publishes it. Nothing flows back.

**Fire-and-forget is the entire contract, and it is stronger than alerts.notify()'s.** That
one may block for up to ten seconds; this one may not block at all, because it runs once per
tick inside the loop whose timing IS the measurement. So the socket is non-blocking, every
failure is swallowed, and only a CHANGE of state is logged -- a sidecar that is down for a day
must not write 43,200 identical lines into sigen.log. A datagram socket is what makes "no
listener" cheap: there is no connection to hold, nothing to reconnect, and a send to a path
with nobody behind it fails immediately with ENOENT or ECONNREFUSED. A full receive queue
fails immediately too (EAGAIN on Linux, ENOBUFS on macOS), so a wedged sidecar costs the
logger one failed syscall per tick and never a wait.

**Why the plan hash travels in every datagram.** A record is only decodable against the
manifest of the block plan that wrote it -- the same reason decode.py refuses to cross a
plan-hash mismatch. A sidecar that cached one manifest at startup would, after a retune,
decode the new plan's bytes at the old plan's offsets: plausible numbers, all wrong. Eight
bytes per tick buys the sidecar the ability to notice instead.

The record itself is byte-for-byte what Archive.write() put on disk, so the live stream and
the archive cannot disagree about what a tick contained.
"""
import os
import socket
import stat

MAGIC = b"SGL1"                 # sigen live, framing version 1
HASH_LEN = 8                    # log.plan_hash() is 8 hex characters
PREFIX_LEN = len(MAGIC) + HASH_LEN

# sun_path is 108 bytes on Linux and 104 on macOS, including the terminating NUL. Under the
# smaller, with margin, so a path that works on the workstation also works on the Pi.
MAX_PATH_BYTES = 100


def frame(plan_hash, rec):
    """The datagram for one archive record."""
    h = plan_hash.encode("ascii")
    if len(h) != HASH_LEN:
        raise ValueError(f"plan hash {plan_hash!r} is not {HASH_LEN} characters")
    return MAGIC + h + rec


def parse(datagram):
    """(plan_hash, record bytes) from one datagram. Raises ValueError on anything else."""
    if len(datagram) < PREFIX_LEN or not datagram.startswith(MAGIC):
        raise ValueError("not a live-tap datagram")
    h = datagram[len(MAGIC):PREFIX_LEN]
    try:
        plan_hash = h.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("plan hash is not ASCII")
    return plan_hash, datagram[PREFIX_LEN:]


def path_problem(path):
    """Why `path` cannot be a socket address, or None."""
    if not path:
        return "no socket path configured"
    if len(path.encode()) > MAX_PATH_BYTES:
        return (f"socket path is {len(path.encode())} bytes; a Unix socket address must be "
                f"under {MAX_PATH_BYTES}")
    return None


class Tap:
    """The sending half. Construct once per capture run; `send()` once per record.

    Never raises, from the constructor or from send(). If the socket cannot even be created
    the tap is simply inert, and says so once.
    """

    def __init__(self, path, plan_hash, log=None):
        self.path = path
        self.say = log or (lambda *_a, **_k: None)
        self.sent = self.dropped = 0
        self.ok = None              # None until the first send settles it
        self.sock = None
        self.prefix = b""
        try:
            problem = path_problem(path)
            if problem:
                raise ValueError(problem)
            self.prefix = frame(plan_hash, b"")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.setblocking(False)
            self.sock = s
        except Exception as e:
            self._note(f"  [live] tap disabled: {type(e).__name__}: {e} -- capture is "
                       f"unaffected")

    def send(self, rec):
        """Offer one record. Returns True if the kernel took it. Never raises, never waits."""
        try:
            if self.sock is None:
                return False
            try:
                self.sock.sendto(self.prefix + rec, self.path)
            except OSError as e:
                self.dropped += 1
                if self.ok is not False:
                    self.ok = False
                    self._note(f"  [live] tap not delivering to {self.path} "
                               f"({_reason(e)}); records are dropped, capture is "
                               f"unaffected")
                return False
            self.sent += 1
            if self.ok is not True:
                self.ok = True
                self._note(f"  [live] tap delivering to {self.path}")
            return True
        except Exception:
            # Anything not foreseen above -- and the contract is that capture cannot see it.
            return False

    def close(self):
        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def _note(self, line):
        try:
            self.say(line)
        except Exception:
            pass


def _reason(e):
    """A short, stable description: the state-change line should read the same each time."""
    if isinstance(e, FileNotFoundError):
        return "no socket there; is the live unit running?"
    if isinstance(e, ConnectionRefusedError):
        return "nothing listening on the socket"
    if isinstance(e, (BlockingIOError,)) or getattr(e, "errno", None) in (55, 105):
        # EAGAIN on Linux; ENOBUFS (55 macOS, 105 Linux) when the receive queue is full.
        return "the sidecar is not keeping up; its queue is full"
    return f"{type(e).__name__}: {e}"


def _in_use(path):
    """True if something is bound to the socket at `path`. A stale one refuses the connect."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def bind(path):
    """The receiving half: a bound datagram socket at `path`, mode 600.

    Replaces a stale socket left by a previous run -- the normal state after a crash -- but
    refuses to delete anything that is not a socket, so a mistyped path cannot remove a file.
    """
    problem = path_problem(path)
    if problem:
        raise ValueError(problem)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not stat.S_ISSOCK(st.st_mode):
            raise FileExistsError(f"{path} exists and is not a socket; refusing to replace it")
        if _in_use(path):
            # Unlinking it would silently steal the stream from the process that owns it --
            # `live.py --stdout` started by hand next to the running unit, say.
            raise FileExistsError(f"{path} is in use by another receiver (is the live unit "
                                  f"running?); stop it first")
        os.unlink(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.bind(path)
        os.chmod(path, 0o600)
    except Exception:
        s.close()
        raise
    return s
