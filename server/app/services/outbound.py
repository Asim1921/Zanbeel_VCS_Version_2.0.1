"""Requests the server makes to URLs that people configure.

Webhooks and push-rule policy services both make the server send an HTTP request to a
URL a repository owner typed. The server sits inside the network, so without limits
that URL is a way to make it call things the owner cannot reach: its own loopback
port, other machines on the LAN, or the cloud metadata service at 169.254.169.254.
The webhook delivery log shows the reply, so the owner could also read what came back.

The rules, following GitLab's defaults for webhooks and integrations:

* Only ``http`` and ``https``.
* The host is resolved, and every address it resolves to must be public. Loopback,
  link-local (which includes the metadata service), private, shared (CGNAT), multicast,
  reserved and unspecified addresses are refused.
* ``FOXNEST_OUTBOUND_ALLOWED_HOSTS`` lists exceptions: host names (``ci.internal``,
  ``*.corp.example``), addresses or CIDR ranges (``10.0.5.0/24``). Anything listed is
  allowed whatever it resolves to; this is how an on-premises CI server is reached.
* ``FOXNEST_OUTBOUND_ALLOW_PRIVATE=1`` allows private and shared ranges wholesale.
  Loopback and link-local still need to be listed explicitly.
* The connection goes to the address that was checked, with TLS still verified against
  the host name, so a DNS answer that changes between the check and the connection
  (DNS rebinding) cannot send the request somewhere else.
* Redirects are never followed: a redirect is a second, unchecked destination.
* Replies are read up to a limit, so a receiver cannot exhaust memory.

Settings are read on every call rather than at import, so an administrator's change
takes effect on restart and tests can set them per case.
"""

import ipaddress
import os
import socket
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

MAX_RESPONSE_BYTES = 64 * 1024

_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")   # CGNAT, RFC 6598


class OutboundBlocked(Exception):
    """The destination is not one the server may call. The message says why."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Target:
    url: str
    scheme: str
    host: str
    port: int
    path: str
    addresses: List[str] = field(default_factory=list)
    username: Optional[str] = None
    password: Optional[str] = None


# --- settings ---------------------------------------------------------------------------

def _allow_private() -> bool:
    return (os.environ.get("FOXNEST_OUTBOUND_ALLOW_PRIVATE") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _allowlist() -> Tuple[List[str], List[ipaddress._BaseNetwork]]:
    """(host names, networks) from FOXNEST_OUTBOUND_ALLOWED_HOSTS."""
    names, networks = [], []
    for raw in (os.environ.get("FOXNEST_OUTBOUND_ALLOWED_HOSTS") or "").split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            names.append(entry.rstrip("."))
    return names, networks


def _host_allowlisted(host: str, names: List[str]) -> bool:
    host = host.lower().rstrip(".")
    for name in names:
        if name.startswith("*.") and (host.endswith(name[1:]) or host == name[2:]):
            return True
        if host == name:
            return True
    return False


# --- classification ---------------------------------------------------------------------

def blocked_reason(address: str, allow_private: Optional[bool] = None) -> Optional[str]:
    """Why ``address`` may not be called, or None when it may."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if allow_private is None:
        allow_private = _allow_private()

    if ip.is_loopback:
        return "a loopback address (this server itself)"
    if ip.is_link_local:
        return "a link-local address (this includes the cloud metadata service)"
    if ip.is_unspecified:
        return "an unspecified address"
    if ip.is_multicast:
        return "a multicast address"
    private = ip.is_private or (isinstance(ip, ipaddress.IPv4Address) and ip in _SHARED_ADDRESS_SPACE)
    if private and not ip.is_reserved:
        return None if allow_private else "on a private network"
    if ip.is_reserved or not ip.is_global:
        return "a reserved address"
    return None


def resolve(url: str, *, resolve_dns: bool = True) -> Target:
    """Parse and check ``url``. Raises OutboundBlocked when it may not be called.

    With ``resolve_dns`` False only the URL itself is checked (scheme, and the host when
    it is a literal address) -- for validating configuration where DNS may not answer.
    """
    parts = urlsplit((url or "").strip())
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise OutboundBlocked("The URL must start with http:// or https://")
    host = (parts.hostname or "").strip()
    if not host:
        raise OutboundBlocked("The URL has no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise OutboundBlocked("The URL has an invalid port")

    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    target = Target(url=url, scheme=scheme, host=host, port=port, path=path,
                    username=parts.username, password=parts.password)

    names, networks = _allowlist()
    host_listed = _host_allowlisted(host, names)

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        target.addresses = [str(literal)]
    elif resolve_dns:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            raise OutboundBlocked(f"The host '{host}' could not be resolved")
        seen = []
        for info in infos:
            address = info[4][0].split("%", 1)[0]     # drop an IPv6 zone id
            if address not in seen:
                seen.append(address)
        target.addresses = seen
    else:
        return target

    if host_listed:
        return target

    for address in target.addresses:
        if any(ipaddress.ip_address(address) in network for network in networks):
            continue
        reason = blocked_reason(address)
        if reason:
            raise OutboundBlocked(
                f"'{host}' resolves to {address}, {reason}. The server does not send requests "
                "there. An administrator can allow it with FOXNEST_OUTBOUND_ALLOWED_HOSTS."
            )
    return target


def check_url(url: str) -> Optional[str]:
    """Validate a configured URL. Returns a warning when DNS cannot answer yet.

    A host that does not resolve today is allowed through -- it may only exist on the
    network the deliveries will come from -- and is checked again before every request.
    """
    try:
        resolve(url)
        return None
    except OutboundBlocked as exc:
        if "could not be resolved" not in exc.message:
            raise
        resolve(url, resolve_dns=False)
        return exc.message + "; it will be checked again before each delivery."


# --- sending ----------------------------------------------------------------------------

class _PinnedAdapter(HTTPAdapter):
    """Connects to an address already checked, while TLS still verifies the host name."""

    def __init__(self, hostname: str, **kwargs):
        self._hostname = hostname
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["server_hostname"] = self._hostname
        kwargs["assert_hostname"] = self._hostname
        super().init_poolmanager(*args, **kwargs)


def post(url: str, *, data: bytes, headers: dict, timeout: float) -> requests.Response:
    """POST to ``url`` under the rules above. Raises OutboundBlocked or a requests error.

    The returned response holds at most MAX_RESPONSE_BYTES of body.
    """
    target = resolve(url)
    address = target.addresses[0]
    netloc = f"[{address}]" if ":" in address else address
    default_port = 443 if target.scheme == "https" else 80
    pinned_url = f"{target.scheme}://{netloc}:{target.port}{target.path}"

    send_headers = dict(headers)
    send_headers["Host"] = target.host if target.port == default_port else f"{target.host}:{target.port}"

    session = requests.Session()
    # Proxy settings from the environment would send the request somewhere other than
    # the address that was checked.
    session.trust_env = False
    if target.scheme == "https":
        session.mount("https://", _PinnedAdapter(target.host))
    auth = (target.username, target.password or "") if target.username else None
    try:
        response = session.post(
            pinned_url, data=data, headers=send_headers, timeout=timeout,
            allow_redirects=False, stream=True, auth=auth,
        )
        body = response.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True) or b""
        response._content = body[:MAX_RESPONSE_BYTES]
        response._content_consumed = True
        return response
    finally:
        session.close()
