"""Authenticated etcd transactions for the two data hosts and their voting witness."""

import base64
import json
import re
import ssl
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit

from cirra.domain import CirraError, canonical, fields, number, string

_OWNER = ContextVar("cirra_publishing_owner", default=None)
OWNER_KEY = "worker-owner"
MAX_REPLY = 2 * 1024 * 1024


class QuorumUnavailable(CirraError):
    """No member could confirm the requested operation on the pinned cluster."""


class OwnershipLost(QuorumUnavailable):
    """An old worker must abandon the current pass and obtain a new session."""


def current_owner():
    return _OWNER.get()


@contextmanager
def bind_owner(owner):
    token = _OWNER.set(owner)
    try:
        yield
    finally:
        _OWNER.reset(token)


@dataclass(frozen=True)
class QuorumConfig:
    endpoints: tuple
    namespace: str
    cluster_id: str
    ca: Path
    certificate: Path
    private_key: Path
    routing_key_env: str
    node: str
    preferred_node: str
    standby_node: str
    lease_seconds: int
    recovery_archive: Path | None = None

    @classmethod
    def load(cls, root, data):
        from cirra.service_config import env_name, local_path

        fields(
            data,
            [
                "endpoints",
                "namespace",
                "cluster_id",
                "ca",
                "certificate",
                "private_key",
                "routing_key_env",
                "node",
                "preferred_node",
                "standby_node",
            ],
            ["lease_seconds", "recovery_archive"],
            label="coordination",
        )
        urls = data["endpoints"]
        if not isinstance(urls, list) or len(urls) != 3:
            raise CirraError("coordination needs three distinct HTTPS voter endpoints")
        endpoints = []
        for raw in urls:
            try:
                url = urlsplit(string(raw, "voter URL", 320))
            except ValueError as exc:
                raise CirraError("invalid voter URL") from exc
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.path not in ("", "/")
                or url.query
                or url.fragment
            ):
                raise CirraError("voters require HTTPS origins without credentials, paths or query")
            try:
                port = url.port
            except ValueError as exc:
                raise CirraError("invalid voter port") from exc
            endpoints.append((url.hostname.lower(), 443 if port is None else port))
        if len(set(endpoints)) != 3 or any(port == 0 for _, port in endpoints):
            raise CirraError("coordination needs three distinct HTTPS voter endpoints")
        for key in ("namespace", "node", "preferred_node", "standby_node"):
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", string(data[key], key)):
                raise CirraError("coordination names must be lowercase slugs")
        participants = {data["preferred_node"], data["standby_node"]}
        if len(participants) != 2 or data["node"] not in participants:
            raise CirraError("only the two distinct configured data hosts can run Cirra")
        cluster = string(data["cluster_id"], "etcd decimal cluster ID", 20)
        if not re.fullmatch(r"[1-9][0-9]{0,19}", cluster) or int(cluster) >= 2**64:
            raise CirraError("invalid etcd decimal cluster ID")
        return cls(
            tuple(endpoints),
            data["namespace"],
            cluster,
            *(local_path(root, data[key]) for key in ("ca", "certificate", "private_key")),
            env_name(data["routing_key_env"]),
            data["node"],
            data["preferred_node"],
            data["standby_node"],
            number(data.get("lease_seconds", 15), "ownership TTL", 5, 120, integral=True),
            local_path(root, data["recovery_archive"]) if "recovery_archive" in data else None,
        )


def _b64(value):
    return base64.b64encode(value.encode()).decode("ascii")


def _decode(value):
    return base64.b64decode(value, validate=True).decode()


class Quorum:
    def __init__(self, config):
        self.config = config
        self.prefix = "/cirra/" + config.namespace + "/"
        self.context = ssl.create_default_context(cafile=str(config.ca))
        self.context.load_cert_chain(str(config.certificate), str(config.private_key))
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2

    def _key(self, key):
        return _b64(self.prefix + key)

    def _post(self, connection, path, data):
        connection.request(
            "POST", "/v3/" + path, canonical(data).encode(), {"Content-Type": "application/json"}
        )
        reply = connection.getresponse()
        raw = reply.read(MAX_REPLY + 1)
        if reply.status != 200 or len(raw) > MAX_REPLY:
            raise QuorumUnavailable("voter did not confirm a bounded response")
        obj = json.loads(raw)
        header = obj.get("header") if isinstance(obj, dict) else None
        if not isinstance(header, dict) or str(header.get("cluster_id")) != self.config.cluster_id:
            raise QuorumUnavailable("voter returned an invalid or different cluster identity")
        return obj

    def call(self, path, data):
        for host, port in self.config.endpoints:
            connection = HTTPSConnection(host, port, context=self.context, timeout=1.5)
            try:
                # Verify the cluster before each mutation on this same TLS connection.
                if path not in ("kv/range", "maintenance/status"):
                    self._post(connection, "kv/range", {"key": self._key("identity")})
                    if connection.sock is None:
                        raise QuorumUnavailable("voter closed the verified TLS connection")
                return self._post(connection, path, data)
            except (HTTPException, OSError, ValueError, TypeError, QuorumUnavailable):
                # Every write below is a conditional transaction or an expiring lease grant.
                continue
            finally:
                connection.close()
        raise QuorumUnavailable("no voter confirmed the operation on the configured cluster")

    def get(self, key):
        request_key = self._key(key)
        items = self.call("kv/range", {"key": request_key}).get("kvs", [])
        if items == []:
            return None
        try:
            if len(items) == 1 and items[0]["key"] == request_key:
                return _decode(items[0]["value"])
        except (TypeError, KeyError, ValueError):
            pass
        raise QuorumUnavailable("voter returned an invalid single-key result")

    def scan(self, prefix, after="", limit=32):
        """One bounded lexicographic page in the service namespace."""
        if (
            not prefix.endswith("/")
            or (after and not after.startswith(prefix))
            or type(limit) is not int
            or not 1 <= limit <= 256
        ):
            raise ValueError("invalid quorum scan cursor")
        start = after + "\0" if after else prefix
        end = prefix[:-1] + "0"
        response = self.call(
            "kv/range",
            {
                "key": self._key(start),
                "range_end": self._key(end),
                "limit": str(limit),
                "sort_order": "ASCEND",
                "sort_target": "KEY",
            },
        )
        result = []
        items, more = response.get("kvs", []), response.get("more", False)
        if not isinstance(items, list) or len(items) > limit or type(more) is not bool:
            raise QuorumUnavailable("voter returned an invalid range page")
        previous = after
        try:
            for item in items:
                key = _decode(item["key"])
                short = key[len(self.prefix) :]
                if not key.startswith(self.prefix + prefix) or short <= previous:
                    raise QuorumUnavailable("unexpected key in the requested range")
                result.append((short, _decode(item["value"])))
                previous = short
        except (TypeError, KeyError, ValueError) as exc:
            raise QuorumUnavailable("voter returned an invalid range entry") from exc
        return result, more

    def compare(self, checks, puts, *, deletes=(), leases=None):
        compare = []
        for key, expected in checks.items():
            condition = {"key": self._key(key), "result": "EQUAL"}
            if expected is None:
                condition.update(target="VERSION", version="0")
            else:
                condition.update(target="VALUE", value=_b64(expected))
            compare.append(condition)
        changes = []
        for key, value in puts.items():
            put = {"key": self._key(key), "value": _b64(value)}
            if leases and key in leases:
                put["lease"] = leases[key]
            changes.append({"request_put": put})
        changes.extend({"request_delete_range": {"key": self._key(key)}} for key in deletes)
        result = self.call("kv/txn", {"compare": compare, "success": changes})
        return result.get("succeeded") is True

    def lease(self):
        result = self.call("lease/grant", {"TTL": str(self.config.lease_seconds)})
        lease = str(result.get("ID", ""))
        if not re.fullmatch(r"[1-9][0-9]*", lease):
            raise QuorumUnavailable("voter returned an invalid lease")
        return lease

    def acquire(self):
        if self.get(OWNER_KEY) is not None:
            return None
        owner = Owner(self)
        lease = self.lease()
        if self.compare({OWNER_KEY: None}, {OWNER_KEY: owner.token}, leases={OWNER_KEY: lease}):
            return owner
        # A committed transaction can lose its response. The nonce belongs to this call only.
        return owner if self.get(OWNER_KEY) == owner.token else None


class Owner:
    def __init__(self, quorum):
        self.quorum = quorum
        self.token = uuid.uuid4().hex
        self.lost = threading.Event()

    def get(self, key):
        return self.quorum.get(key)

    def compare(self, checks, puts, *, deletes=()):
        if OWNER_KEY in checks or OWNER_KEY in puts or OWNER_KEY in deletes:
            raise ValueError("ownership key is reserved")
        if self.lost.is_set():
            raise OwnershipLost("worker no longer owns this generation")
        ok = self.quorum.compare({OWNER_KEY: self.token, **checks}, puts, deletes=deletes)
        if not ok and self.quorum.get(OWNER_KEY) != self.token:
            self.lost.set()
            raise OwnershipLost("worker no longer owns this generation")
        return ok

    def renew(self):
        lease = self.quorum.lease()
        if not self.quorum.compare(
            {OWNER_KEY: self.token}, {OWNER_KEY: self.token}, leases={OWNER_KEY: lease}
        ):
            self.lost.set()
            raise OwnershipLost("worker lease expired")

    @contextmanager
    def maintaining(self):
        stop = threading.Event()

        def renew():
            while not stop.wait(self.quorum.config.lease_seconds / 3):
                try:
                    self.renew()
                except (OSError, CirraError):
                    self.lost.set()
                    return

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            with bind_owner(self):
                yield self
        finally:
            stop.set()
            thread.join(timeout=6)
            self.lost.set()
            # A process can die here. The lease also releases ownership without this call.
            try:
                self.quorum.compare({OWNER_KEY: self.token}, {}, deletes=[OWNER_KEY])
            except QuorumUnavailable:
                pass
