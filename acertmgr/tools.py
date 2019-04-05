#!/usr/bin/env python
# -*- coding: utf-8 -*-

# acertmgr - various support functions
# Copyright (c) Markus Hauschild & David Klaftenegger, 2016.
# Copyright (c) Rudolf Mayerhofer, 2019.
# available under the ISC license, see LICENSE

import base64
import binascii
import datetime
import io
import os
import sys
import traceback

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.x509.oid import NameOID, ExtensionOID

try:
    from urllib.request import urlopen, Request  # Python 3
except ImportError:
    from urllib2 import urlopen, Request  # Python 2


class InvalidCertificateError(Exception):
    pass


# @brief a simple, portable indent function
def indent(text, spaces=0):
    ind = ' ' * spaces
    return os.linesep.join(ind + line for line in text.splitlines())


# @brief wrapper for log output
def log(msg, exc=None, error=False, warning=False):
    if error:
        prefix = "Error: "
    elif warning:
        prefix = "Warning: "
    else:
        prefix = ""

    output = prefix + msg
    if exc:
        _, exc_value, _ = sys.exc_info()
        if not getattr(exc, '__traceback__', None) and exc == exc_value:
            # Traceback handling on Python 2 is ugly, so we only output it if the exception is the current sys one
            formatted_exc = traceback.format_exc()
        else:
            formatted_exc = traceback.format_exception(type(exc), exc, getattr(exc, '__traceback__', None))
        exc_string = ''.join(formatted_exc) if isinstance(formatted_exc, list) else str(formatted_exc)
        output += os.linesep + indent(exc_string, len(prefix))

    if error or warning:
        sys.stderr.write(output + os.linesep)
        sys.stderr.flush()  # force flush buffers after message was written for immediate display
    else:
        sys.stdout.write(output + os.linesep)
        sys.stdout.flush()  # force flush buffers after message was written for immediate display


# @brief wrapper for downloading an url
def get_url(url, data=None, headers=None):
    return urlopen(Request(url, data=data, headers={} if headers is None else headers))


# @brief check whether existing certificate is still valid or expiring soon
# @param crt_file string containing the path to the certificate file
# @param ttl_days the minimum amount of days for which the certificate must be valid
# @return True if certificate is still valid for at least ttl_days, False otherwise
def is_cert_valid(cert, ttl_days):
    now = datetime.datetime.now()
    if cert.not_valid_before > now:
        raise InvalidCertificateError("Certificate seems to be from the future")

    expiry_limit = now + datetime.timedelta(days=ttl_days)
    if cert.not_valid_after < expiry_limit:
        return False

    return True


# @brief create a certificate signing request
# @param names list of domain names the certificate should be valid for
# @param key the key to use with the certificate in pyopenssl format
# @param must_staple whether or not the certificate should include the OCSP must-staple flag
# @return the CSR in pyopenssl format
def new_cert_request(names, key, must_staple=False):
    primary_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                                 names[0].decode('utf-8') if getattr(names[0], 'decode', None) else
                                                 names[0])])
    all_names = x509.SubjectAlternativeName(
        [x509.DNSName(name.decode('utf-8') if getattr(name, 'decode', None) else name) for name in names])
    req = x509.CertificateSigningRequestBuilder()
    req = req.subject_name(primary_name)
    req = req.add_extension(all_names, critical=False)
    if must_staple:
        if getattr(x509, 'TLSFeature', None):
            req = req.add_extension(x509.TLSFeature(features=[x509.TLSFeatureType.status_request]), critical=False)
        else:
            log('OCSP must-staple ignored as current version of cryptography does not support the flag.', warning=True)
    req = req.sign(key, hashes.SHA256(), default_backend())
    return req


# @brief generate a new account key
# @param path path where the new key file should be written in PEM format (optional)
def new_account_key(path=None, key_size=4096):
    return new_ssl_key(path, key_size)


# @brief generate a new ssl key
# @param path path where the new key file should be written in PEM format (optional)
def new_ssl_key(path=None, key_size=4096):
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
        backend=default_backend()
    )
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    )
    if path is not None:
        with io.open(path, 'wb') as pem_out:
            pem_out.write(pem)
        try:
            os.chmod(path, int("0400", 8))
        except OSError:
            log('Could not set file permissions on {0}!'.format(path), warning=True)
    return private_key


# @brief read a key from file
# @param path path to file
# @param key indicate whether we are loading a key
# @param csr indicate whether we are loading a csr
# @return the key in pyopenssl format
def read_pem_file(path, key=False, csr=False):
    with io.open(path, 'r') as f:
        if key:
            return serialization.load_pem_private_key(f.read().encode('utf-8'), None, default_backend())
        elif csr:
            return x509.load_pem_x509_csr(f.read().encode('utf8'), default_backend())
        else:
            return convert_pem_str_to_cert(f.read())


# @brief write cert data to PEM formatted file
def write_pem_file(crt, path, perms=None):
    with io.open(path, "w") as f:
        f.write(convert_cert_to_pem_str(crt))
    if perms:
        try:
            os.chmod(path, perms)
        except OSError:
            log('Could not set file permissions ({0}) on {1}!'.format(perms, path), warning=True)


# @brief download the issuer ca for a given certificate
# @param cert certificate data
# @returns ca certificate data
def download_issuer_ca(cert):
    aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS)
    ca_issuers = None
    for data in aia.value:
        if data.access_method == x509.OID_CA_ISSUERS:
            ca_issuers = data.access_location.value
            break

    if not ca_issuers:
        log("Could not determine issuer CA for given certificate: {}".format(cert), error=True)
        return None

    log("Downloading CA certificate from {}".format(ca_issuers))
    resp = get_url(ca_issuers)
    code = resp.getcode()
    if code >= 400:
        log("Could not download issuer CA (error {}) for given certificate: {}".format(code, cert), error=True)
        return None

    return x509.load_der_x509_certificate(resp.read(), default_backend())


# @brief determine all san domains on a given certificate
def get_cert_domains(cert):
    san_cert = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    domains = set()
    domains.add(cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value)
    if san_cert:
        for d in san_cert.value:
            domains.add(d.value)
    # Convert IDNA domain to correct representation and return the list
    return [x.encode('idna').decode('ascii') if any(ord(c) >= 128 for c in x) else x for x in domains]


# @brief determine certificate cn
def get_cert_cn(cert):
    return "CN={}".format(cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value)


# @brief determine certificate end of validity
def get_cert_valid_until(cert):
    return cert.not_valid_after


# @brief convert certificate to PEM format
# @param cert certificate object in pyopenssl format
# @return the certificate in PEM format
def convert_cert_to_pem_str(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode('utf8')


# @brief load a PEM certificate from str
def convert_pem_str_to_cert(certdata):
    return x509.load_pem_x509_certificate(certdata.encode('utf8'), default_backend())


# @brief serialize cert/csr to DER bytes
def convert_cert_to_der_bytes(data):
    return data.public_bytes(serialization.Encoding.DER)


# @brief load a DER certificate from str
def convert_der_bytes_to_cert(data):
    return x509.load_der_x509_certificate(data, default_backend())


# @brief determine key signing algorithm and jwk data
# @return key algorithm, signature algorithm, key numbers as a dict
def get_key_alg_and_jwk(key):
    if isinstance(key, rsa.RSAPrivateKey):
        # See https://tools.ietf.org/html/rfc7518#section-6.3
        numbers = key.public_key().public_numbers()
        return "RS256", {"kty": "RSA",
                         "e": bytes_to_base64url(number_to_byte_format(numbers.e)),
                         "n": bytes_to_base64url(number_to_byte_format(numbers.n))}
    else:
        raise ValueError("Unsupported key: {}".format(key))


# @brief sign string with key
def signature_of_str(key, string):
    alg, _ = get_key_alg_and_jwk(key)
    data = string.encode('utf8')
    if alg == 'RS256':
        return key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    else:
        raise ValueError("Unsupported signature algorithm: {}".format(alg))


# @brief hash a string
def hash_of_str(string):
    account_hash = hashes.Hash(hashes.SHA256(), backend=default_backend())
    account_hash.update(string.encode('utf8'))
    return account_hash.finalize()


# @brief helper function to base64 encode for JSON objects
# @param b the byte-string to encode
# @return the encoded string
def bytes_to_base64url(b):
    return base64.urlsafe_b64encode(b).decode('utf8').replace("=", "")


# @brief convert numbers to byte-string
# @param num number to convert
# @return byte-string containing the number
def number_to_byte_format(num):
    n = format(num, 'x')
    n = "0{0}".format(n) if len(n) % 2 else n
    return binascii.unhexlify(n)


# @brief check whether existing target file is still valid or source crt has been updated
# @param target string containing the path to the target file
# @param file string containing the path to the certificate file
# @return True if target file is at least as new as the certificate, False otherwise
def target_is_current(target, file):
    if not os.path.isfile(target):
        return False
    target_date = os.path.getmtime(target)
    crt_date = os.path.getmtime(file)
    return target_date >= crt_date
