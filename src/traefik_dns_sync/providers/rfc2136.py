"""RFC 2136 Dynamic DNS Update provider.

Uses TSIG-authenticated DNS UPDATE messages to manage A and TXT records,
similar to external-dns's RFC 2136 provider.
"""

from __future__ import annotations

import logging

import dns.name
import dns.query
import dns.rdatatype
import dns.resolver
import dns.tsigkeyring
import dns.update

from traefik_dns_sync.config import Rfc2136Config
from traefik_dns_sync.models import DnsRecord, TxtRecord

logger = logging.getLogger(__name__)

TSIG_ALGORITHMS = {
    "hmac-sha256": dns.tsig.HMAC_SHA256,
    "hmac-sha512": dns.tsig.HMAC_SHA512,
    "hmac-md5": dns.tsig.HMAC_MD5,
    "hmac-sha1": dns.tsig.HMAC_SHA1,
    "hmac-sha224": dns.tsig.HMAC_SHA224,
    "hmac-sha384": dns.tsig.HMAC_SHA384,
}


class Rfc2136Provider:
    """Manage DNS records via RFC 2136 Dynamic Updates with TSIG auth."""

    def __init__(self, config: Rfc2136Config) -> None:
        self._config = config
        self._server = config.host
        self._port = config.port
        self._zone = dns.name.from_text(config.zone)

        # Build TSIG keyring
        algorithm = TSIG_ALGORITHMS.get(config.tsig_key_algorithm.lower())
        if not algorithm:
            raise ValueError(
                f"Unsupported TSIG algorithm: {config.tsig_key_algorithm}"
                f" (supported: {', '.join(TSIG_ALGORITHMS)})"
            )
        self._algorithm = algorithm
        self._keyring = dns.tsigkeyring.from_text({
            config.tsig_key_name: config.tsig_key_secret,
        })

    @property
    def name(self) -> str:
        return "rfc2136"

    def _make_update(self) -> dns.update.Update:
        return dns.update.Update(
            self._zone,
            keyring=self._keyring,
            keyalgorithm=self._algorithm,
        )

    def _send(self, update: dns.update.Update) -> dns.message.Message:
        return dns.query.tcp(update, self._server, port=self._port, timeout=10)

    def _fqdn_dot(self, fqdn: str) -> str:
        """Ensure FQDN ends with a dot for DNS wire format."""
        if not fqdn.endswith("."):
            return fqdn + "."
        return fqdn

    async def get_records(self, domain_filter: list[str] | None = None) -> list[DnsRecord]:
        """Query the DNS zone for A records via AXFR."""
        records: list[DnsRecord] = []
        try:
            xfr = dns.query.xfr(self._server, self._zone, port=self._port, timeout=10,
                                 keyring=self._keyring, keyalgorithm=self._algorithm)
            for message in xfr:
                for rrset in message.answer:
                    if rrset.rdtype != dns.rdatatype.A:
                        continue
                    fqdn = str(rrset.name).rstrip(".")
                    for rdata in rrset:
                        record = DnsRecord.from_fqdn(fqdn, str(rdata))
                        if domain_filter and not any(
                            record.fqdn.endswith(f".{d}") or record.fqdn == d
                            for d in domain_filter
                        ):
                            continue
                        records.append(record)
        except Exception:
            logger.exception("AXFR failed for zone %s — falling back to empty", self._zone)

        return records

    async def create_record(self, record: DnsRecord) -> str | None:
        """Create an A record via DNS UPDATE."""
        update = self._make_update()
        fqdn = self._fqdn_dot(record.fqdn)
        update.add(fqdn, 300, dns.rdatatype.A, record.ip)

        response = self._send(update)
        rcode = response.rcode()
        if rcode != dns.rcode.NOERROR:
            raise RuntimeError(f"DNS UPDATE failed for {record.fqdn}: rcode={dns.rcode.to_text(rcode)}")

        logger.info("Created RFC2136 A record: %s -> %s", record.fqdn, record.ip)
        return None  # No provider-specific ID for DNS

    async def update_record(self, record: DnsRecord, provider_id: str | None = None) -> None:
        """Update an A record by replacing it."""
        update = self._make_update()
        fqdn = self._fqdn_dot(record.fqdn)
        update.replace(fqdn, 300, dns.rdatatype.A, record.ip)

        response = self._send(update)
        rcode = response.rcode()
        if rcode != dns.rcode.NOERROR:
            raise RuntimeError(f"DNS UPDATE failed for {record.fqdn}: rcode={dns.rcode.to_text(rcode)}")

        logger.info("Updated RFC2136 A record: %s -> %s", record.fqdn, record.ip)

    async def delete_record(self, record: DnsRecord, provider_id: str | None = None) -> None:
        """Delete an A record."""
        update = self._make_update()
        fqdn = self._fqdn_dot(record.fqdn)
        update.delete(fqdn, dns.rdatatype.A, record.ip)

        response = self._send(update)
        rcode = response.rcode()
        if rcode != dns.rcode.NOERROR:
            raise RuntimeError(f"DNS UPDATE failed for {record.fqdn}: rcode={dns.rcode.to_text(rcode)}")

        logger.info("Deleted RFC2136 A record: %s", record.fqdn)

    async def create_txt_record(self, record: TxtRecord) -> str | None:
        """Create a TXT ownership record."""
        update = self._make_update()
        fqdn = self._fqdn_dot(record.fqdn)
        update.add(fqdn, 300, dns.rdatatype.TXT, record.value)

        response = self._send(update)
        rcode = response.rcode()
        if rcode != dns.rcode.NOERROR:
            raise RuntimeError(f"DNS UPDATE failed for TXT {record.fqdn}: rcode={dns.rcode.to_text(rcode)}")

        logger.info("Created RFC2136 TXT record: %s", record.fqdn)
        return None

    async def delete_txt_record(
        self, record: TxtRecord, provider_id: str | None = None,
    ) -> None:
        """Delete a TXT ownership record."""
        update = self._make_update()
        fqdn = self._fqdn_dot(record.fqdn)
        # Delete all TXT records for this name
        update.delete(fqdn, dns.rdatatype.TXT)

        response = self._send(update)
        rcode = response.rcode()
        if rcode != dns.rcode.NOERROR:
            raise RuntimeError(f"DNS UPDATE failed for TXT {record.fqdn}: rcode={dns.rcode.to_text(rcode)}")

        logger.info("Deleted RFC2136 TXT record: %s", record.fqdn)

    async def get_txt_records(
        self, prefix: str, domain_filter: list[str] | None = None,
    ) -> list[TxtRecord]:
        """Fetch TXT ownership records via AXFR."""
        records: list[TxtRecord] = []
        txt_prefix = f"{prefix}.a-"

        try:
            xfr = dns.query.xfr(self._server, self._zone, port=self._port, timeout=10,
                                 keyring=self._keyring, keyalgorithm=self._algorithm)
            for message in xfr:
                for rrset in message.answer:
                    if rrset.rdtype != dns.rdatatype.TXT:
                        continue
                    fqdn = str(rrset.name).rstrip(".")
                    if not fqdn.startswith(txt_prefix):
                        continue

                    for rdata in rrset:
                        # TXT rdata is a tuple of byte strings
                        value = b"".join(rdata.strings).decode()

                        if domain_filter:
                            a_fqdn = TxtRecord.extract_a_record_fqdn(fqdn, prefix)
                            if a_fqdn and not any(
                                a_fqdn.endswith(f".{d}") or a_fqdn == d
                                for d in domain_filter
                            ):
                                continue

                        records.append(TxtRecord(fqdn=fqdn, value=value))
        except Exception:
            logger.exception("AXFR failed for TXT records in zone %s", self._zone)

        return records
