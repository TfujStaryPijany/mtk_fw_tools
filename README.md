# mtk_fw_tools

Set of tools to unpack/repack Mediatek firmware. Work in progress.

The primary focus of developing these tools was to decompress the ALICE partition from Mediatek firmware blobs in order to inspect/reverse engineer the software on various devices (smart watches, GSM trackers, cheap phones, etc.).

A typical firmware dump consists of:
+ Internal bootloader
+ External bootloader (EXT_BOOTLOADER)
+ Kernel (ROM)
+ User partition (VIVA header)
    + ZIMGE partition (ZIMAGE_ER - LZMA compressed resources)
    + BOOT_ZIMAGE partition (usually empty)
    + DCMCMP partition (LZMA compressed parts)
    + ALICE partition (ALICE_1/ALICE_2 - main firmware, compressed)

The main (non-kernel) firmware of the device seems to lie in the ALICE partition which is range encoded and bitpacked ARM instructions. Until now this encoding was not public.

Briefly, the encoder performs the following steps:

1. Read instructions from ALICE.bin (16-bits ARM Thumb)
2. Translate BL/BLX addresses
3. Add instructions, in order of appearance, to a binary tree
4. Generate dictionary (histogram)
5. Range encode instructions
6. Bitpack range encoded instructions
7. Generate mapping table (one 32-bit entry per even block: bits 0..23 are
   the block address, bits 24..31 are `((block length in bytes) - 13) << 2`)
8. Postprocess, prepend header, append mapping table and dictionary, etc

The decoder must do the reverse. In short, we read the compressed data as a bit string, looking up each instruction in the dictionary to retrieve the uncompressed version as we proceed. The mapping table tells us when we have reached a block of $blocksize (typically 64 bytes, 32 instructions), at which point we skip to the next start-of-byte and proceed. Presumably this lets the firmware decode segments of the code as needed, without loading the entire image into memory.

# Tools

+ alice.py - partial prototype of a from scratch packer. It bitpacks the
  instruction stream but does not generate the mapping table, the dictionary
  or the header, so its output is not a loadable ALICE image. Use
  `alice_pack.py` for repacking, or ALICE.exe.
+ alice_pack.py - repack a decoded stream into a valid ALICE_2 image, reusing
  the dictionary and range registers of a reference image. Verified by a byte
  exact round trip.
+ unalice.py - unpack ALICE partition (ALICE_1 and ALICE_2). The end of stream
  is derived from the mapping table rather than guessed, every even block is
  checked against its mapping entry while decoding, and there is no longer a
  dependency on `bitstring`.

# Usage

## Requirements
    python (or python3)
    python-bitstring (or python3-bitstring)

If you have the firmware of your device, open it in a hex editor and search for the ALICE_1 or ALICE_2 string.

<pre>
...
0017FEB0   DF 5A 8A AC  22 D7 FE 6F  01 6E 98 E8  79 36 7C 50  .Z.."..o.n..y6|P
0017FEC0   82 5E 25 DD  26 B9 20 A1  67 17 F9 2D  CD 54 EC 08  .^%.&. .g..-.T..
0017FED0   E0 A8 06 1D  A7 88 DB 9C  61 92 6E 75  1B 3D 1D 00  ........a.nu.=..
0017FEE0   41 4C 49 43  45 5F 32 00  08 FF 17 10  D0 92 2C 10  ALICE_2.......,.
0017FEF0   0C 6A 2D 10  04 00 06 00  07 00 08 00  0A 00 0B 00  .j-.............
0017FF00   0C 00 09 01  40 00 FF FF  E8 28 7D 15  2C 5B 2D 68  ....@....(}.,[-h
0017FF10   3F D5 F6 DF  E0 BB 8C CA  C2 D6 38 1E  05 33 9F CB  ?.........8..3..
0017FF20   04 96 CC 6D  35 7E F0 F4  20 3C B8 46  0E 79 0E 27  ...m5~.. <.F.y.'
0017FF30   9D 87 A6 4E  78 E2 03 B0  42 82 16 0C  CC 19 98 33  ...Nx...B......3
...
</pre>

Cut the section out using `dd` (0x17FEE0 == 1572576):

```
$ dd if=firmware.bin of=ALICE bs=1 skip=1572576
```

Run unalice on the resulting file:

```
$ python3 unalice.py ALICE
```

Load the resulting `alice-py.bin` into your favourite disassembler!

If BL/BLX targets seem to not make sense in the disassembler, try using the `-t` option with `unalice.py`.

### Repacking

`alice_pack.py` encodes a decoded stream back into an ALICE_2 image. It takes
the dictionary, the range registers and the block size from a reference image,
so the two unsolved parts of from scratch packing, the dictionary histogram
ordering and dynamic range registers, do not come into play. Prefix 7 is a 19
bit literal escape, so instructions missing from the dictionary still encode.

```
$ python3 unalice.py -t ALICE out.bin      # decode
$ ...                                      # patch out.bin
$ python3 alice_pack.py out.bin ALICE ALICE.new
```

Use the same `-t` setting for decoding and patching: without `-t` the decoder
rewrites BL/BLX targets, and the packer expects the stream in its stored form.

On an `ALICE_2` image of 2014800 bytes, 19537 mapping entries, 39074 blocks of
32 instructions, decoding and then repacking with no changes reproduces the
input byte for byte. Patching a string and an instruction that is absent from
the dictionary, so that it takes the literal escape, also survives a round
trip, with the mapping table addresses shifting as expected.

Two details that matter for a valid image: the compressed region is padded so
that the mapping table starts on a 4 byte boundary, since its entries are 32
bit words, and the block address is a 24 bit field, so it has to be masked
before being combined with the flag byte.
