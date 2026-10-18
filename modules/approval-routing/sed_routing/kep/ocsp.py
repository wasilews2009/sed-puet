"""Перевірка відкликання сертифіката через OCSP КНЕДП (RFC 6960).

Будь-яка неоднозначність (немає адреси OCSP, мережа, непідписана або застаріла відповідь,
алгоритм ДСТУ у відповіді) повертає UNKNOWN — верифікатор тоді не приймає підпис.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509 import ocsp
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtendedKeyUsageOID

from .models import RevocationStatus


def _ocsp_url(cert: x509.Certificate) -> str | None:
    try:
        aia = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
    except x509.ExtensionNotFound:
        return None
    for d in aia:
        if d.access_method == AuthorityInformationAccessOID.OCSP:
            return d.access_location.value
    return None


def _verify_signed(resp: ocsp.OCSPResponse, signer: x509.Certificate) -> bool:
    key = signer.public_key()
    try:
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(resp.signature, resp.tbs_response_bytes, padding.PKCS1v15(), resp.signature_hash_algorithm)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(resp.signature, resp.tbs_response_bytes, ec.ECDSA(resp.signature_hash_algorithm))
        else:
            return False
    except (InvalidSignature, UnsupportedAlgorithm, TypeError, ValueError):
        return False
    return True


class OcspRevocationChecker:
    def __init__(self, client: httpx.AsyncClient, *, max_age: timedelta = timedelta(hours=24)) -> None:
        self.client, self.max_age = client, max_age

    async def status(self, cert: x509.Certificate, issuer: x509.Certificate, at: datetime) -> RevocationStatus:
        url = _ocsp_url(cert)
        if url is None:
            return RevocationStatus.UNKNOWN
        req = ocsp.OCSPRequestBuilder().add_certificate(cert, issuer, hashes.SHA256()).build()
        try:
            r = await self.client.post(url, content=req.public_bytes(serialization.Encoding.DER),
                                       headers={"Content-Type": "application/ocsp-request"})
            r.raise_for_status()
            resp = ocsp.load_der_ocsp_response(r.content)
        except (httpx.HTTPError, ValueError):
            return RevocationStatus.UNKNOWN
        if resp.response_status is not ocsp.OCSPResponseStatus.SUCCESSFUL:
            return RevocationStatus.UNKNOWN
        try:
            if resp.serial_number != cert.serial_number:
                return RevocationStatus.UNKNOWN
            # Підписує або сам КНЕДП, або делегований респондер з EKU OCSPSigning, виданий ним.
            signer = issuer
            for c in resp.certificates:
                if c.issuer == issuer.subject:
                    try:
                        eku = c.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
                        c.verify_directly_issued_by(issuer)
                    except (x509.ExtensionNotFound, InvalidSignature, ValueError, TypeError):
                        continue
                    if ExtendedKeyUsageOID.OCSP_SIGNING in eku:
                        signer = c
            if not _verify_signed(resp, signer):
                return RevocationStatus.UNKNOWN
            if resp.this_update_utc > at + timedelta(minutes=5) or resp.this_update_utc < at - self.max_age:
                return RevocationStatus.UNKNOWN
            if resp.next_update_utc is not None and resp.next_update_utc < at:
                return RevocationStatus.UNKNOWN
        except (ValueError, UnsupportedAlgorithm):
            return RevocationStatus.UNKNOWN
        if resp.certificate_status is ocsp.OCSPCertStatus.GOOD:
            return RevocationStatus.GOOD
        if resp.certificate_status is ocsp.OCSPCertStatus.REVOKED:
            return RevocationStatus.REVOKED
        return RevocationStatus.UNKNOWN
