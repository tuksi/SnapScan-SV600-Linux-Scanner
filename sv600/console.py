"""Terminal progress display for the command-line tools (implements the scanner progress API)."""
import sys
import threading
import time


class ConsoleProgress:
    """Numbered steps, a transfer progress bar and spinners for waits.

    Falls back to plain one-line-per-event output when stderr is not a terminal.
    """
    SPIN = '|/-\\'

    def __init__(self, total_steps, verbose=False):
        self.total = total_steps
        self.n = 0
        self.tty = sys.stderr.isatty()
        self.verbose = verbose
        self.t_step = self.t_start = time.time()
        self.open = False        # a step line is waiting for its "done"

    def _w(self, s):
        sys.stderr.write(s)
        sys.stderr.flush()

    def step(self, text):
        self.end()
        self.n += 1
        self.t_step = time.time()
        self.label = f'[{self.n}/{self.total}] {text}'
        self._w(f'{self.label} ...')
        self.open = True
        if not self.tty or self.verbose:
            self._w('\n')

    def end(self, note=''):
        if not self.open:
            return
        dt = time.time() - self.t_step
        msg = f'done ({dt:.1f} s){", " + note if note else ""}'
        if self.tty and not self.verbose:
            self._w(f'\r\033[K{self.label} ... {msg}\n')
        else:
            self._w(f'    {msg}\n')
        self.open = False

    def detail(self, text):
        """Extra line under the current step (verbose command log)."""
        if self.verbose:
            self._w(f'      {text}\n')

    def spin(self, text):
        """Update a spinner line; call repeatedly while waiting."""
        if not self.tty or self.verbose:
            return
        dt = time.time() - self.t_step
        c = self.SPIN[int(dt * 8) % 4]
        self._w(f'\r\033[K{self.label} ... {c} {text} {dt:4.1f} s')

    def bar(self, done, total):
        if not self.tty or self.verbose:
            return
        dt = max(time.time() - self.t_step, 1e-6)
        frac = min(done / total, 1.0) if total else 0
        width = 30
        fill = int(frac * width)
        rate = done / dt / 1e6
        eta = (total - done) / (done / dt) if done else 0
        self._w(f'\r\033[K{self.label}  [{"#" * fill}{"." * (width - fill)}] {frac * 100:5.1f}%  '
                f'{done / 1e6:5.1f}/{total / 1e6:.1f} MB  {rate:4.1f} MB/s  ETA {eta:3.0f} s')

    def run_with_spinner(self, text, fn, *args, **kw):
        """Run fn in a thread, animating a spinner until it returns."""
        result, error = [], []

        def target():
            try:
                result.append(fn(*args, **kw))
            except BaseException as e:   # re-raised in the main thread
                error.append(e)
        th = threading.Thread(target=target, daemon=True)
        th.start()
        while th.is_alive():
            self.spin(text)
            th.join(0.125)
        if error:
            raise error[0]
        return result[0] if result else None

    def finish(self, text):
        self.end()
        self._w(f'{text} (total {time.time() - self.t_start:.1f} s)\n')
