"""纯 Python AES（仅实现解密，供 CBC 解密光猫 SuPassword 密文）。

零第三方依赖：只用标准库。AES 分组解密覆盖 AES-128/192/256，
对外暴露 cbc_decrypt（不做去填充，交由调用方按需处理）与 pkcs7_unpad。

自测：`python3 scripts/lib/aes.py` 用 FIPS-197 附录 C 已知答案校验 AES-128/256 解密。
"""

# 标准 AES S-box（FIPS-197）
_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]

# 逆 S-box（由 S-box 反查得到）
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i


def _xtime(a):
    """GF(2^8) 乘 2。"""
    a &= 0xFF
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1) & 0xFF


def _gmul(a, b):
    """GF(2^8) 乘。"""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


class AES:
    """AES 分组解密（支持 128/192/256 位密钥）。"""

    def __init__(self, key):
        if len(key) not in (16, 24, 32):
            raise ValueError("密钥长度必须是 16/24/32 字节")
        self.nk = len(key) // 4
        self.nr = self.nk + 6
        self._w = self._expand_key(key)

    def _expand_key(self, key):
        nk, nr = self.nk, self.nr
        w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
        rcon = 1
        for i in range(nk, 4 * (nr + 1)):
            temp = list(w[i - 1])
            if i % nk == 0:
                temp = temp[1:] + temp[:1]           # RotWord
                temp = [_SBOX[b] for b in temp]      # SubWord
                temp[0] ^= rcon
                rcon = _xtime(rcon)
            elif nk > 6 and i % nk == 4:
                temp = [_SBOX[b] for b in temp]      # AES-256 额外的 SubWord
            w.append([w[i - nk][j] ^ temp[j] for j in range(4)])
        return w

    @staticmethod
    def _add_round_key(s, w, rnd):
        for c in range(4):
            kw = w[rnd * 4 + c]
            for r in range(4):
                s[4 * c + r] ^= kw[r]

    @staticmethod
    def _inv_shift_rows(s):
        out = list(s)
        for r in range(1, 4):
            for c in range(4):
                out[4 * c + r] = s[4 * ((c - r) % 4) + r]
        return out

    @staticmethod
    def _inv_mix_columns(s):
        out = list(s)
        for c in range(4):
            a = s[4 * c:4 * c + 4]
            out[4 * c + 0] = _gmul(a[0], 0x0e) ^ _gmul(a[1], 0x0b) ^ _gmul(a[2], 0x0d) ^ _gmul(a[3], 0x09)
            out[4 * c + 1] = _gmul(a[0], 0x09) ^ _gmul(a[1], 0x0e) ^ _gmul(a[2], 0x0b) ^ _gmul(a[3], 0x0d)
            out[4 * c + 2] = _gmul(a[0], 0x0d) ^ _gmul(a[1], 0x09) ^ _gmul(a[2], 0x0e) ^ _gmul(a[3], 0x0b)
            out[4 * c + 3] = _gmul(a[0], 0x0b) ^ _gmul(a[1], 0x0d) ^ _gmul(a[2], 0x09) ^ _gmul(a[3], 0x0e)
        return out

    def decrypt_block(self, block):
        """解密单个 16 字节分组（ECB 单块）。"""
        if len(block) != 16:
            raise ValueError("分组必须是 16 字节")
        s = list(block)
        w, nr = self._w, self.nr
        self._add_round_key(s, w, nr)
        for rnd in range(nr - 1, 0, -1):
            s = self._inv_shift_rows(s)
            s = [_INV_SBOX[b] for b in s]
            self._add_round_key(s, w, rnd)
            s = self._inv_mix_columns(s)
        s = self._inv_shift_rows(s)
        s = [_INV_SBOX[b] for b in s]
        self._add_round_key(s, w, 0)
        return bytes(s)


def cbc_decrypt(key, iv, data):
    """AES-CBC 解密。data 长度须为 16 的整数倍。不去填充（由调用方处理）。"""
    if len(iv) != 16:
        raise ValueError("IV 必须是 16 字节")
    if len(data) % 16 != 0:
        raise ValueError("密文长度必须是 16 的整数倍")
    cipher = AES(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        dec = cipher.decrypt_block(blk)
        out += bytes(x ^ y for x, y in zip(dec, prev))
        prev = blk
    return bytes(out)


def pkcs7_unpad(data):
    """去除 PKCS#7 填充。填充非法时原样返回（避免误截断真实数据）。"""
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


def _selftest():
    """FIPS-197 附录 C 已知答案（AES-128 / AES-256 ECB 解密）。"""
    # AES-128
    k128 = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    c128 = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    p128 = bytes.fromhex("00112233445566778899aabbccddeeff")
    assert cbc_decrypt(k128, bytes(16), c128) == p128, "AES-128 KAT 失败"
    # AES-256
    k256 = bytes.fromhex("000102030405060708090a0b0c0d0e0f"
                         "101112131415161718191a1b1c1d1e1f")
    c256 = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")
    assert cbc_decrypt(k256, bytes(16), c256) == p128, "AES-256 KAT 失败"
    # SP 800-38A F.2.5（AES-256-CBC 已知答案，覆盖 CBC 链接）
    kcbc = bytes.fromhex("603deb1015ca71be2b73aef0857d7781"
                         "1f352c073b6108d72d9810a30914dff4")
    ivcbc = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    ctcbc = bytes.fromhex("f58c4c04d6e5f1ba779eabfb5f7bfbd6")
    ptcbc = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    assert cbc_decrypt(kcbc, ivcbc, ctcbc) == ptcbc, "AES-256-CBC KAT 失败"
    # PKCS#7 去填充
    assert pkcs7_unpad(b"abc" + bytes([13]) * 13) == b"abc", "PKCS#7 去填充失败"
    print("[OK] AES 自测通过（FIPS-197 AES-128/256 已知答案 + PKCS#7）")
    # 可选：与本机 pycryptodome 对拍（仅开发期，非依赖）
    try:
        from Crypto.Cipher import AES as _CAES
        key = bytes(range(32))
        iv = bytes(range(16))
        pt = b"router-supassword-" + bytes(range(16))  # 32 字节
        enc = _CAES.new(key, _CAES.MODE_CBC, iv).encrypt(pt)
        assert cbc_decrypt(key, iv, enc) == pt, "与 pycryptodome 对拍失败"
        print("[OK] 与 pycryptodome CBC 对拍一致")
    except ImportError:
        pass


if __name__ == "__main__":
    _selftest()
