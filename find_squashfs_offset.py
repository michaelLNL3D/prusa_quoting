"""
Find the byte offset of the squashfs filesystem embedded in an AppImage.
Validates each candidate against the squashfs 4.0 superblock structure
so we don't return false positives from ELF code sections.
"""
import sys
import struct

SQUASHFS_MAGIC_LE = b'hsqs'   # little-endian (standard x86_64 Linux)
SQUASHFS_MAGIC_BE = b'sqsh'   # big-endian (rare)
VALID_COMPRESSION = {1, 2, 3, 4, 5, 6}   # gzip lzma lzo xz lz4 zstd


def is_valid_squashfs(data, offset):
    """Return True if data[offset:] is a real squashfs 4.0 superblock."""
    if offset + 96 > len(data):
        return False
    sb = data[offset:offset + 96]
    magic = sb[0:4]
    if magic not in (SQUASHFS_MAGIC_LE, SQUASHFS_MAGIC_BE):
        return False
    endian = '<' if magic == SQUASHFS_MAGIC_LE else '>'
    try:
        inode_count = struct.unpack_from(endian + 'I', sb, 4)[0]
        block_size  = struct.unpack_from(endian + 'I', sb, 12)[0]
        compress_id = struct.unpack_from(endian + 'H', sb, 20)[0]
        s_major     = struct.unpack_from(endian + 'H', sb, 28)[0]
        bytes_used  = struct.unpack_from(endian + 'Q', sb, 40)[0]
    except struct.error:
        return False
    if s_major != 4:
        return False
    if not (1 <= inode_count <= 10_000_000):
        return False
    if not (4096 <= block_size <= 1_048_576):
        return False
    if block_size & (block_size - 1) != 0:   # must be power of 2
        return False
    if compress_id not in VALID_COMPRESSION:
        return False
    if bytes_used < 4096:
        return False
    return True


path = sys.argv[1]
with open(path, 'rb') as f:
    data = f.read()

# Collect all candidate offsets (search past first 50 KB to skip ELF header)
candidates = []
for magic in (SQUASHFS_MAGIC_LE, SQUASHFS_MAGIC_BE):
    pos = 50_000
    while True:
        idx = data.find(magic, pos)
        if idx == -1:
            break
        candidates.append(idx)
        pos = idx + 1

candidates.sort()

for offset in candidates:
    if is_valid_squashfs(data, offset):
        print(offset)
        sys.exit(0)

print("ERROR: no valid squashfs superblock found", file=sys.stderr)
sys.exit(1)
