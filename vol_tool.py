import struct, os, sys, glob

def read_vol(path):
    b = open(path, 'rb').read()
    if b[:4] != b'PVOL':
        return None
    dirofs = struct.unpack('<I', b[4:8])[0]
    if b[dirofs:dirofs+4] != b'vols':
        return None
    nsize = struct.unpack('<I', b[dirofs+4:dirofs+8])[0]
    names_blk = b[dirofs+8:dirofs+8+nsize]
    ipos = dirofs + 8 + nsize
    # align/find voli
    ipos = b.find(b'voli', ipos-4)
    isize = struct.unpack('<I', b[ipos+4:ipos+8])[0]
    idx = b[ipos+8:ipos+8+isize]
    files = {}
    for off in range(0, len(idx) - 16, 17):
        _z, name_ofs, data_ofs, size = struct.unpack('<4I', idx[off:off+16])
        end = names_blk.find(b'\0', name_ofs)
        name = names_blk[name_ofs:end].decode('latin-1')
        files[name] = (data_ofs, size)
    return b, files

def extract(volpath, name, outdir):
    b, files = read_vol(volpath)
    data_ofs, size = files[name]
    # data at offset may be VBLK-wrapped
    if b[data_ofs:data_ofs+4] == b'VBLK':
        payload = b[data_ofs+8:data_ofs+8+size]
    else:
        payload = b[data_ofs:data_ofs+size]
    out = os.path.join(outdir, name)
    open(out, 'wb').write(payload)
    return out

if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'find':
        pat = sys.argv[2].lower()
        for vol in glob.glob(r'C:\Dynamix\Tribes\base\*.vol'):
            try:
                r = read_vol(vol)
            except Exception:
                r = None
            if not r:
                continue
            _, files = r
            for n in files:
                if pat in n.lower():
                    print(os.path.basename(vol), n, files[n])
    elif cmd == 'extract':
        vol, name, outdir = sys.argv[2:5]
        print(extract(vol, name, outdir))

def extract_pl98(volpath, outpath):
    """Carve the PL98 multi-palette block out of a *World.vol into a .ppl."""
    import struct as _s
    b = open(volpath, 'rb').read()
    p = b.find(b'PL98')
    if p == -1:
        return None
    count = _s.unpack('<I', b[p+8:p+12])[0]
    end = min(len(b), p + 1076 + 2064 * count)
    open(outpath, 'wb').write(b[p:end])
    return outpath
