"""Fill in `FORGEJO_ROOT_URL` with this machine's address, before `up`.

Forgejo builds clone URLs and the links in everything it sends from that value,
so `localhost` there hands every other machine a URL pointing back at itself —
silently, and only for other people. It has to be right before Forgejo starts,
which makes it a question `up` would otherwise have to ask.

It is not a question. The machine knows its own address, and the one answer that
is ever wanted is the interface that carries the default route. Detect it, write
it, say what was written. `.env.sample` ships a placeholder rather than a working
`localhost` so that "not set yet" is a state this can recognise.

Runs on the host, like the rest of the setup commands: it writes `.env`, which no
container can reach, and it must work before there is a stack at all.
"""
import socket
import sys

from . import models

# Values that mean "nobody has chosen an address yet", as opposed to a LAN
# address someone typed deliberately. A localhost is in this list on purpose:
# it is never a useful answer for a stack whose whole point is that other
# machines reach it, and leaving it alone would just preserve the bug.
UNSET = ("", "http://<ip-address>:23000/", "<ip-address>")
LOOPBACK = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def lan_address():
    """This machine's address on the network that carries its default route.

    A UDP socket is the portable way to ask. Connecting one sends nothing — it
    only makes the OS pick a source interface — and reading the local end back
    is then the address other machines would see. Enumerating interfaces instead
    is what produces the classic wrong answer on Windows, where the Hyper-V and
    WSL switches look exactly as good as the real NIC.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(1)
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1: routable, never answers
        address = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    if not address or address.startswith("127.") or address in LOOPBACK:
        return None
    return address


def host_of(url):
    """The host out of `http://host:port/`, without importing a URL parser for
    one field that this file also writes."""
    rest = url.split("//", 1)[-1]
    return rest.split("/", 1)[0].split(":", 1)[0]


def needs_setting(current):
    if current is None or current.strip() in UNSET:
        return True
    return host_of(current.strip()) in LOOPBACK


def ensure(out, ask=None):
    """Put a usable address in `.env`, and return it. Never prompts."""
    current = models.env_value("FORGEJO_ROOT_URL")
    if not needs_setting(current):
        return current

    address = lan_address()
    if not address:
        out("address: this machine has no address on a network — leaving")
        out("         FORGEJO_ROOT_URL alone. Set it by hand in .env, or other")
        out("         machines will be handed clone URLs pointing at themselves.")
        return current

    port = models.env_value("FORGEJO_PORT") or "23000"
    url = "http://%s:%s/" % (address, port)
    models.env_set("FORGEJO_ROOT_URL", url)
    out("address: FORGEJO_ROOT_URL=%s" % url)
    out("         (this machine's address on the network it routes through; it")
    out("         is what Forgejo puts in clone URLs, so it has to be the one")
    out("         other machines use. Edit .env if that is the wrong interface.)")
    return url


def main(argv=None):
    models._force_utf8()
    out = lambda line: print(line, flush=True)
    try:
        ensure(out)
    except models.ModelError as exc:
        # Never fatal. This is a convenience ahead of `up`, and `up` failing
        # because a nicety could not run would be the worse trade.
        out("address: %s" % exc)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
