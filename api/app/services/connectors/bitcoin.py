"""Watch-only Bitcoin wallet connector (BIP32/44/49/84 local derivation + Mempool.space)."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from dataclasses import dataclass
import hashlib
import hmac
import re
from typing import Optional

from ecdsa import SECP256k1
from ecdsa.ellipticcurve import Point
import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.schemas import SyncResult
from app.services.connectors import ConnectorConfigError, ConnectorUpstreamError
from app.services.ingestion_service import (
    BankFeedGateway,
    ExternalAccountSpec,
    ProviderAccountFeed,
    sync_provider_feeds,
)

MEMPOOL_BASE_URL = "https://mempool.space/api"
SATOSHIS_PER_BTC = 100_000_000
DEFAULT_GAP_LIMIT = 20

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

_XPUB_VERSIONS = {
    bytes.fromhex("0488b21e"): "p2pkh",       # xpub -> 1...
    bytes.fromhex("049d7cb2"): "p2sh-p2wpkh", # ypub -> 3...
    bytes.fromhex("04b24746"): "p2wpkh",      # zpub -> bc1q...
}
_STATIC_ADDR_RE = re.compile(r"^(bc1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{11,71}|[13][1-9A-HJ-NP-Za-km-z]{25,34})$")


@dataclass(frozen=True)
class WatchSource:
    """Normalized watch-only Bitcoin source (extended public key or static address)."""

    label: str
    value: str
    kind: str  # "xpub" or "address"


# ============================================================================
# 2. PURE CRYPTO & NORMALIZATION CORE (Zero I/O)
# ============================================================================
def _hash160(data: bytes) -> bytes:
    sha = hashlib.sha256(data).digest()
    return hashlib.new("ripemd160", sha).digest()


def _b58encode_check(payload: bytes) -> str:
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    raw = payload + checksum
    num = int.from_bytes(raw, "big")
    chars: list[str] = []
    while num > 0:
        num, rem = divmod(num, 58)
        chars.append(_B58_ALPHABET[rem])
    leading_zeros = len(raw) - len(raw.lstrip(b"\x00"))
    return ("1" * leading_zeros) + "".join(reversed(chars))


def _b58decode_check(value: str) -> bytes:
    num = 0
    for ch in value:
        if ch not in _B58_INDEX:
            raise ConnectorConfigError(f"Invalid Base58 character in watch source: {ch!r}")
        num = num * 58 + _B58_INDEX[ch]
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    leading_zeros = len(value) - len(value.lstrip("1"))
    full = (b"\x00" * leading_zeros) + raw
    if len(full) < 5:
        raise ConnectorConfigError("Invalid Base58Check payload length.")
    payload, checksum = full[:-4], full[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        raise ConnectorConfigError("Base58Check checksum mismatch in extended public key.")
    return payload


def _bech32_polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convert_bits(data: bytes, from_bits: int, to_bits: int, pad: bool = True) -> list[int]:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << to_bits) - 1
    for value in data:
        acc = (acc << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (to_bits - bits)) & maxv)
    return ret


def _encode_segwit_v0(program: bytes, hrp: str = "bc") -> str:
    data = [0] + _convert_bits(program, 8, 5)
    combined = _bech32_hrp_expand(hrp) + data + [0, 0, 0, 0, 0, 0]
    polymod = _bech32_polymod(combined) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return f"{hrp}1" + "".join(_BECH32_CHARSET[d] for d in (data + checksum))


def _decompress_pubkey(compressed: bytes) -> Point:
    if len(compressed) != 33 or compressed[0] not in (2, 3):
        raise ConnectorConfigError("Invalid compressed secp256k1 public key.")
    curve = SECP256k1.curve
    p = curve.p()
    x = int.from_bytes(compressed[1:], "big")
    y_sq = (pow(x, 3, p) + curve.a() * x + curve.b()) % p
    y = pow(y_sq, (p + 1) // 4, p)
    if (y % 2) != (compressed[0] - 2):
        y = p - y
    return Point(curve, x, y)


def _compress_pubkey(point: Point) -> bytes:
    prefix = b"\x02" if (point.y() % 2 == 0) else b"\x03"
    return prefix + int(point.x()).to_bytes(32, "big")


def _ckd_pub(pub_bytes: bytes, chain_code: bytes, index: int) -> tuple[bytes, bytes]:
    """Derive a non-hardened BIP32 child public key (index < 2^31)."""
    if index >= 0x80000000:
        raise ConnectorConfigError("Hardened derivation is unsupported for watch-only public keys.")
    data = pub_bytes + index.to_bytes(4, "big")
    digest = hmac.new(chain_code, data, hashlib.sha512).digest()
    il = int.from_bytes(digest[:32], "big")
    ir = digest[32:]
    if il >= SECP256k1.order:
        raise ConnectorConfigError("Derived BIP32 scalar exceeds curve order.")
    child_point = (il * SECP256k1.generator) + _decompress_pubkey(pub_bytes)
    return _compress_pubkey(child_point), ir


def derive_address_from_xpub(xpub: str, chain: int, index: int) -> str:
    """Derive a Bitcoin address locally from an xpub/ypub/zpub without exposing the key."""
    raw = _b58decode_check(xpub.strip())
    if len(raw) != 78:
        raise ConnectorConfigError("Extended public key must decode to 78 bytes.")
    version = raw[:4]
    script_type = _XPUB_VERSIONS.get(version)
    if not script_type:
        raise ConnectorConfigError("Unsupported extended public key prefix (expected xpub, ypub, or zpub).")

    chain_code = raw[13:45]
    pub_key = raw[45:78]

    branch_pub, branch_chain = _ckd_pub(pub_key, chain_code, chain)
    leaf_pub, _ = _ckd_pub(branch_pub, branch_chain, index)
    pk_hash = _hash160(leaf_pub)

    if script_type == "p2wpkh":
        return _encode_segwit_v0(pk_hash)
    if script_type == "p2sh-p2wpkh":
        redeem_script = b"\x00\x14" + pk_hash
        return _b58encode_check(b"\x05" + _hash160(redeem_script))
    return _b58encode_check(b"\x00" + pk_hash)


def parse_watch_sources(raw: str) -> list[WatchSource]:
    """Parse comma-separated watch-only Bitcoin sources (label:key_or_addr or key_or_addr)."""
    entries = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not entries:
        raise ConnectorConfigError(
            "Bitcoin watch-only source missing: set PLUTUS_BTC_WATCH_SOURCES to an xpub/ypub/zpub or address."
        )

    sources: list[WatchSource] = []
    for idx, entry in enumerate(entries, start=1):
        if ":" in entry:
            label, value = entry.split(":", 1)
            label = label.strip() or f"BitBox02 #{idx}"
            value = value.strip()
        else:
            label = "BitBox02" if len(entries) == 1 else f"BitBox02 #{idx}"
            value = entry

        if value.startswith(("xpub", "ypub", "zpub")):
            _b58decode_check(value)
            sources.append(WatchSource(label=label, value=value, kind="xpub"))
        elif _STATIC_ADDR_RE.match(value):
            sources.append(WatchSource(label=label, value=value, kind="address"))
        else:
            raise ConnectorConfigError(
                f"Unsupported Bitcoin watch source '{label}': expected xpub/ypub/zpub or valid BTC address."
            )
    return sources


def sum_address_satoshis(stats: dict) -> tuple[int, bool]:
    """Return (net_satoshis, has_tx_history) from a mempool.space /api/address payload."""
    chain = stats.get("chain_stats") or {}
    mempool = stats.get("mempool_stats") or {}
    chain_bal = int(chain.get("funded_txo_sum", 0)) - int(chain.get("spent_txo_sum", 0))
    mempool_bal = int(mempool.get("funded_txo_sum", 0)) - int(mempool.get("spent_txo_sum", 0))
    tx_count = int(chain.get("tx_count", 0)) + int(mempool.get("tx_count", 0))
    return chain_bal + mempool_bal, tx_count > 0


def build_external_id(source: WatchSource) -> str:
    """Derive a stable, non-sensitive external_account_id hash for a watch source."""
    digest = hashlib.sha256(source.value.encode("utf-8")).hexdigest()[:16]
    return f"btc:{digest}"


def normalize_bitcoin_feed(
    source: WatchSource,
    total_satoshis: int,
    *,
    addresses_scanned: int,
) -> ProviderAccountFeed:
    """Build an immutable ProviderAccountFeed for a watch-only Bitcoin wallet."""
    btc_balance = round(total_satoshis / SATOSHIS_PER_BTC, 8)
    spec = ExternalAccountSpec(
        provider="bitcoin",
        external_id=build_external_id(source),
        name=source.label,
        institution="Bitcoin On-Chain",
        account_type="crypto",
        tier="growth",
        currency="BTC",
        current_balance=btc_balance,
        snapshot_notes=f"Watch-only on-chain sync ({total_satoshis:,} sats across {addresses_scanned} addresses)",
    )
    return ProviderAccountFeed(account=spec, transactions=())


# ============================================================================
# 3. GATEWAY / ADAPTER (All HTTP I/O)
# ============================================================================
class HttpBitcoinGateway:
    """Read-only HTTP gateway querying mempool.space for locally derived addresses."""

    def __init__(
        self,
        raw_sources: str,
        *,
        base_url: str = MEMPOOL_BASE_URL,
        gap_limit: int = DEFAULT_GAP_LIMIT,
        timeout: float = 15.0,
    ) -> None:
        self._sources = parse_watch_sources(raw_sources)
        self._base_url = base_url.rstrip("/")
        self._gap_limit = max(1, gap_limit)
        self._timeout = timeout

    def fetch_feeds(self, **_kwargs) -> list[ProviderAccountFeed]:
        """Derive addresses locally and fetch on-chain balances from Mempool.space."""
        feeds: list[ProviderAccountFeed] = []
        with httpx.Client(timeout=self._timeout) as client:
            for source in self._sources:
                if source.kind == "address":
                    stats = self._get_address_stats(client, source.value)
                    sats, _ = sum_address_satoshis(stats)
                    feeds.append(normalize_bitcoin_feed(source, sats, addresses_scanned=1))
                else:
                    sats, scanned = self._scan_xpub(client, source.value)
                    feeds.append(normalize_bitcoin_feed(source, sats, addresses_scanned=scanned))
        return feeds

    def _get_address_stats(self, client: httpx.Client, address: str) -> dict:
        resp = client.get(f"{self._base_url}/address/{address}")
        if resp.status_code >= 400:
            raise ConnectorUpstreamError(
                f"Mempool.space /address/{address} failed ({resp.status_code}): {resp.text[:200]}"
            )
        return resp.json()

    def _scan_xpub(self, client: httpx.Client, xpub: str) -> tuple[int, int]:
        total_sats = 0
        scanned = 0
        for chain in (0, 1):
            consecutive_unused = 0
            index = 0
            while consecutive_unused < self._gap_limit:
                addr = derive_address_from_xpub(xpub, chain, index)
                stats = self._get_address_stats(client, addr)
                sats, has_history = sum_address_satoshis(stats)
                total_sats += sats
                scanned += 1
                if has_history:
                    consecutive_unused = 0
                else:
                    consecutive_unused += 1
                index += 1
        return total_sats, scanned


# ============================================================================
# 4. ORCHESTRATION
# ============================================================================
def sync_bitcoin(
    db: Session,
    *,
    skip_llm: bool = True,
    gateway: Optional[BankFeedGateway] = None,
) -> SyncResult:
    """Sync watch-only Bitcoin wallet balances and NZD AssetSnapshots."""
    resolved_gateway = gateway or HttpBitcoinGateway(settings.btc_watch_sources)
    feeds = resolved_gateway.fetch_feeds()
    return sync_provider_feeds(db, provider="bitcoin", feeds=feeds, skip_llm=skip_llm)
