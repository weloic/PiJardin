"""Which microcontroller boards this Pi talks to, and how to find them on the USB bus.

A board is identified by what its *firmware* says it is, never by what hardware it is. The
firmware sets its USB product string (`board_build.usb_product` in the PlatformIO env) and this
module finds the device node by matching it exactly. Swap the physical board for any other one,
flash the same image, and it resolves identically — which is the whole point: a board is the
`puit` board because the `puit` firmware is on it, not because of which chip or which socket it
is. That is also why VID/PID and USB serial numbers are deliberately not used: both would tie
identity to the hardware and need editing here after a board swap.

`usb_product` below must be the string the firmware actually advertises, byte for byte — it is
compared with `==`, so a differing space or case just misses. Check with
`python -c "from serial.tools import list_ports; print([p.product for p in list_ports.comports()])"`
or `/boards`, which lists every other port it found precisely so a near-miss is obvious.

`/dev/ttyACM*` numbering is enumeration order, not identity, so it is never an identifier —
only the `fallback` below, covering the window between deploying this code and flashing a
firmware that carries the descriptor.

Nothing is persisted or cached. Resolution is a dict lookup plus a scan of `/sys/class/tty`
(a few ms of sysfs reads; no port is opened, so no board is disturbed), redone on every use.
There is therefore no map to go stale and no invalidation to get wrong — cheap enough that
caching it would cost more in complexity than it saves in time.
"""
import os
import logging

from serial.tools import list_ports

log = logging.getLogger(__name__)

## Every board's flash parameters live beside its identity so a different board family can
## bring its own tool without a special case at the call site.
BOARDS = {
    'puit': {
        'usb_product': 'PiJardin Puit',   # exact string set by board_build.usb_product (fw 2.2.0)
        'fallback':    '/dev/ttyACM0',    # drop once the descriptor is confirmed in the field
        'baud':        9600,
        'proto':       2,
        'flash':       {'tool': 'bossac', 'offset': '0x2000', 'touch_baud': 1200},
    },
}


class BoardError(Exception):
    """Base for every failure to locate or identify a board."""

class UnknownBoard(BoardError):
    """Asked about a board that is not in the registry — a programming error."""

class BoardNotFound(BoardError):
    """Nothing on the USB bus advertises this board, and there is no usable fallback."""

class AmbiguousBoard(BoardError):
    """Several ports claim the same board; refusing to guess between them."""


def boards():
    """Every board name in the registry."""
    return sorted(BOARDS)


def config(board):
    """The registry entry for `board`. Raises UnknownBoard if there is none."""
    try:
        return BOARDS[board]
    except KeyError:
        raise UnknownBoard(
            f"No board named {board!r} in the registry; known boards: {', '.join(boards())}."
        ) from None


def _usb_serial_ports():
    """Every USB serial port on the bus. `vid is None` filters out non-USB ttys."""
    return [p for p in list_ports.comports() if p.vid is not None]


def resolve(board):
    """The device node for `board`, found by the product string its firmware advertises.

    Degrades rather than failing outright: when nothing advertises the expected string but
    the registry names a `fallback` node that exists, that node is used and a WARNING logged.
    This is what lets this code be deployed *before* the board is reflashed — the Pi pulls and
    hard-resets every 15 minutes, and the flash itself runs through code that calls this. Once
    the warning stops appearing, the `fallback` key can be removed.

    Raises AmbiguousBoard rather than picking one when two ports claim the same board, and
    BoardNotFound when there is nothing usable at all.
    """
    cfg = config(board)
    want = cfg['usb_product']
    matches = [p for p in _usb_serial_ports() if p.product == want]

    if len(matches) > 1:
        nodes = ', '.join(p.device for p in matches)
        raise AmbiguousBoard(
            f"{len(matches)} ports advertise {want!r} ({nodes}) — two boards are running the "
            f"same firmware role, so which one is the real {board!r} is undecidable. Reflash "
            f"one of them with its own role before continuing.")

    if matches:
        return matches[0].device

    fallback = cfg.get('fallback')
    if fallback and os.path.exists(fallback):
        log.warning(
            f"Nothing on the USB bus advertises {want!r}; falling back to {fallback}, whose "
            f"name is enumeration order and not an identity. Flash the {board} board with a "
            f"firmware that sets its USB product string to stop this warning.")
        return fallback

    detail = f" and the fallback {fallback} does not exist" if fallback else ""
    raise BoardNotFound(
        f"Nothing on the USB bus advertises {want!r}{detail}. Is the {board} board plugged "
        f"in, and flashed with a firmware that sets its USB product string?")


def describe(board):
    """A diagnostic snapshot of `board`: what the USB bus says, without opening anything.

    Deliberately never opens the device. Opening asserts DTR, which resets the board (see
    read_puit.open_arduino) and would corrupt a measurement in flight. Staying at the USB
    layer also means a board whose firmware has hung still appears here — the SAMD21 USB
    stack is interrupt-driven and keeps answering enumeration even with a stuck main loop —
    so "present but not answering" stays distinguishable from "not plugged in".

    Returns a dict with the expected product string, the resolved node (or the error),
    whether the fallback was used, the matching ports and every other USB serial port.
    """
    cfg = config(board)
    want = cfg['usb_product']

    def port_info(p):
        return {'device': p.device, 'product': p.product, 'manufacturer': p.manufacturer,
                'vid': p.vid, 'pid': p.pid, 'serial_number': p.serial_number}

    ports = _usb_serial_ports()
    info = {
        'board':       board,
        'usb_product': want,
        'device':      None,
        'fallback':    cfg.get('fallback'),
        'fallback_used': False,
        'error':       None,
        'matches':     [port_info(p) for p in ports if p.product == want],
        'others':      [port_info(p) for p in ports if p.product != want],
    }

    try:
        info['device'] = resolve(board)
    except BoardError as e:
        info['error'] = str(e)
    else:
        info['fallback_used'] = not any(m['device'] == info['device'] for m in info['matches'])
    return info
