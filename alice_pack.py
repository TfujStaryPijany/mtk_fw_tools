#!/usr/bin/python3
'''
ALICE_2 packer that reuses the dictionary from an existing image.

The packer shipped in mtk_fw_tools carries three open TODOs: "determine
correct sorting of dictionary histogram", "dynamically generate range
registers" and "testing".  The first two only matter if you want to build a
dictionary from scratch.  For repacking a modified image they can be skipped
entirely: keep the original dictionary and range registers, and encode against
them.  Prefix 7 is a 19 bit literal escape, so any 16 bit value can be emitted
even when it is absent from the dictionary.

Format, confirmed on hardware data:

  instruction = 3 bit prefix s, then range_regs[s] bits
                s < 7 -> payload is (dictionary index - lows[s])
                s = 7 -> payload is the literal 16 bit instruction
  lows[s]     = cumulative sum of 2**range_regs[0..s-1]
  blocks      = blocksize/2 instructions each, padded to a byte boundary
  maptable    = one 32 bit entry per EVEN block:
                  bits 0..23  absolute address of that block
                  bits 24..31 ((length of that block in bytes) - 13) << 2
                plus a final sentinel entry with a zero flag byte

The flag byte was the last unknown.  Measured against a decoded stream,
`(flag >> 2) + 13` equals the byte length of the even block for all 19537
entries of the reference image, no exceptions.  That also matches the formula
`(flag >> 2) + (3 * blocksize/2 >> 3) + 1` which the original decompressor
computed but only used as a loop bound.  The field is sized exactly: a worst
case block is 32 instructions times 19 bits = 76 bytes, and 76 - 13 = 63 is
the largest value six bits can hold.

Usage: alice_pack.py <raw.bin> <reference ALICE> <out ALICE>
'''

import struct
import sys


class BitWriter:
    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.nbits = 0

    def write(self, value, n):
        self.acc = (self.acc << n) | (value & ((1 << n) - 1))
        self.nbits += n
        while self.nbits >= 8:
            self.nbits -= 8
            self.out.append((self.acc >> self.nbits) & 0xff)
        self.acc &= (1 << self.nbits) - 1

    def align(self):
        if self.nbits:
            self.out.append((self.acc << (8 - self.nbits)) & 0xff)
            self.acc = 0
            self.nbits = 0

    def __len__(self):
        return len(self.out)


def read_reference(path):
    d = open(path, 'rb').read()
    if d[:7] not in (b'ALICE_1', b'ALICE_2'):
        sys.exit("reference file is not an ALICE image")
    header_size = 40
    base, mapping_off, dict_off = struct.unpack_from('<III', d, 8)
    range_regs = list(struct.unpack_from('<7H', d, 20))
    range_regs.append(16)
    spare, = struct.unpack_from('<H', d, 34)
    blocksize, = struct.unpack_from('<H', d, 36)
    if blocksize == 0:
        blocksize = 64
    do = dict_off - (base - header_size)
    dic = list(struct.unpack_from('<%dH' % ((len(d) - do) // 2), d, do))
    return dict(raw=d, header_size=header_size, base=base,
                range_regs=range_regs, spare=spare, blocksize=blocksize,
                dictionary=dic, orig=d)


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    rawpath, refpath, outpath = sys.argv[1:4]
    raw = open(rawpath, 'rb').read()
    ref = read_reference(refpath)

    rr = ref['range_regs']
    bs = ref['blocksize']
    base = ref['base']
    dic = ref['dictionary']
    per = bs // 2

    pw = [0] + [1 << r for r in rr[:-1]]
    lows = [sum(pw[:s + 1]) for s in range(8)]
    reach = lows[7]                       # indices the decoder can address

    # lowest index wins, which gives the shortest code
    idx_of = {}
    for i in range(min(reach, len(dic))):
        idx_of.setdefault(dic[i], i)

    if len(raw) % 2:
        sys.exit("input stream has an odd byte count")
    instrs = struct.unpack('<%dH' % (len(raw) // 2), raw)
    if len(instrs) % per:
        sys.exit("stream is not a whole number of blocks (%d instructions)" % per)
    nblocks = len(instrs) // per

    bw = BitWriter()
    block_off = []
    literals = 0
    for b in range(nblocks):
        block_off.append(len(bw))
        for k in range(per):
            v = instrs[b * per + k]
            i = idx_of.get(v)
            if i is None:
                bw.write((7 << 16) | v, rr[7] + 3)
                literals += 1
            else:
                s = 0
                while s < 7 and not (lows[s] <= i < lows[s + 1]):
                    s += 1
                bw.write((s << rr[s]) | (i - lows[s]), rr[s] + 3)
        bw.align()
    block_off.append(len(bw))
    comp = bytearray(bw.out)

    # The mapping table that follows holds 32 bit words, so it has to start on
    # a 4 byte boundary.  Pad the compressed region until it does.  These bytes
    # are never read by the decoder.  When a reference image is available and
    # the region length matches, copy its padding so the output can be compared
    # byte for byte.
    pad = (-(ref['header_size'] + len(comp))) % 4
    if pad:
        src = ref['orig'][ref['header_size'] + len(comp):
                          ref['header_size'] + len(comp) + pad]
        comp += src if len(src) == pad else b'\0' * pad
    comp = bytes(comp)

    table = bytearray()
    overflow = 0
    for b in range(0, nblocks, 2):
        blen = block_off[b + 1] - block_off[b]
        flag = blen - 13
        if not 0 <= flag <= 63:
            overflow += 1
            flag = max(0, min(63, flag))
        # The address is a 24 bit field; mask it so the top byte of `base`
        # cannot leak into the flag byte.
        addr = (base + block_off[b]) & 0x00ffffff
        table += struct.pack('<I', addr | ((flag << 2) << 24))
    table += struct.pack('<I', (base + len(comp) - 1) & 0x00ffffff)  # sentinel

    mapping_off = base + len(comp)
    dict_off = mapping_off + len(table)

    hdr = bytearray(ref['header_size'])
    hdr[0:8] = b'ALICE_2\0'
    struct.pack_into('<III', hdr, 8, base, mapping_off, dict_off)
    for i, r in enumerate(rr[:7]):
        struct.pack_into('<H', hdr, 20 + i * 2, r)
    struct.pack_into('<H', hdr, 34, ref['spare'])
    struct.pack_into('<H', hdr, 36, bs)
    struct.pack_into('<H', hdr, 38, 0xffff)

    out = bytes(hdr) + comp + bytes(table) + struct.pack(
        '<%dH' % len(dic), *dic)
    open(outpath, 'wb').write(out)

    print("instructions %d, blocks %d (of %d), literals %d"
          % (len(instrs), nblocks, per, literals))
    if pad:
        print("padding to a 4 byte boundary: %d byte(s)" % pad)
    print("compressed %d B, maptable %d B (%d entries), dictionary %d B"
          % (len(comp), len(table), len(table) // 4, len(dic) * 2))
    if overflow:
        print("!! %d blocks overflowed the flag field, clamped" % overflow)
    print("wrote %s, %d B" % (outpath, len(out)))

    o = ref['orig']
    if len(o) == len(out):
        if o == out:
            print("IDENTICAL to the reference, byte for byte")
        else:
            first = next(i for i in range(len(o)) if o[i] != out[i])
            print("size matches, first difference at offset %#x" % first)
    else:
        print("size %d vs reference %d" % (len(out), len(o)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
