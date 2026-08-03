try:
    from OpenSSL import crypto
except ModuleNotFoundError:  # pragma: no cover - exercised when pyOpenSSL is absent
    crypto = None


def generate_self_signed_cert(cert_file="cert.pem", key_file="key.pem"):
    """生成自签名 TLS 证书（10年有效）。"""
    if crypto is None:
        raise RuntimeError("缺少 pyOpenSSL 依赖，请执行: pip install pyopenssl")

    k = crypto.PKey()
    k.generate_key(crypto.TYPE_RSA, 2048)

    cert = crypto.X509()
    cert.get_subject().C = "CN"
    cert.get_subject().O = "Nova Chain"
    cert.get_subject().CN = "Nova Super Node"
    cert.set_serial_number(1000)
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(10 * 365 * 24 * 60 * 60)
    cert.set_issuer(cert.get_subject())
    cert.set_pubkey(k)
    cert.sign(k, 'sha256')

    with open(cert_file, "wb") as f:
        f.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
    with open(key_file, "wb") as f:
        f.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, k))

    print(f"证书已生成: {cert_file}, {key_file}")


if __name__ == "__main__":
    generate_self_signed_cert()