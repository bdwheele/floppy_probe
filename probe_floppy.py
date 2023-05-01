#!/bin/env python3
# Use fluxengine software using greaseweazle hardware to probe a floppy in a drive
import argparse
import logging
import os
import subprocess
import tempfile

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", default=False, action="store_true", help="Turn on debugging")
    parser.add_argument("--drive", choices=["A", "a", "B", "b"], default="A", help="Which drive to probe")
    parser.add_argument("--tracks", type=int, choices=[40, 80], default=80, help="Number of tracks the drive is capable of")
    parser.add_argument("--size", type=str, choices=["3.5", "5.25"], default="3.5", help="Media size: 3.5, 5.25")
    parser.add_argument("--device", help="Greaseweazle device name")

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    fluxengine_params = {
        'device': args.device,
        'drive': 0 if args.drive.lower() == 'a' else 1,
        'media_size': args.size,
        'forty_track': args.tracks == 40,
    }

    for probe in (probe_bpb, probe_mac, probe_amiga, probe_c64):
        format, filesystem = probe(fluxengine_params, args.debug)
        if format is not None:
            print(f"Format: {format}, Filesystem: {filesystem}")
            exit(0)

    print("Not a common format or it is corrupt")


def probe_bpb(fe_params, debug):
    """
    IBM-PC formatted disks have a BIOS Parameter block in the first block of the
    media which describes what format the media is.  So, read the first track and
    interpret the BPB
    """    
    bpb_probes = {
        '3.5': {'ibm1440': 2880, 'ibm720': 1440},
        '5.25': {'ibm1200': 2400, 'ibm360': 720, 'ibm320': 640, 'ibm180': 360, 'ibm160': 320}
    }
    
    filesystem = None
    for format in (bpb_probes[fe_params['media_size']]):
        logging.debug(f"Probing for {format}")
        data = read_track(format, 0, fe_params)
        if not has_data(data):
            logging.debug(f"Track appears empty.  Skipping")
            continue        
        if debug:
            dump_data(data, 512)

        if get_word(data, 0x1fe) == 0xaa55:
            logging.debug("Found PC boot sector signature")
            if data[0] in (0xeb, 0xe9):
                logging.debug("Boot sector jump found")
                #bpb_total_sectors = data[0x13] + data[0x14] * 256
                bpb_total_sectors = get_word(data, 0x13)
                logging.debug(f"Total logical sectors: {bpb_total_sectors}")
                if bpb_total_sectors == bpb_probes[fe_params['media_size']][format]:            
                    logging.debug(f"Correct total logical sectors for {format}")
                    # now that we have a valid FAT-ish thing for this bpb, let's
                    # figure out which specific FAT we have.  It's all based
                    # on the number of clusters:  < 4085=FAT12, <65525=FAT16, else FAT32
                    bpb_sectors_per_cluster = data[0x0d]
                    if bpb_sectors_per_cluster not in (1, 2, 4, 8, 16, 32, 64, 128):
                        logging.debug(f"Sectors per cluster has a bogus value: {bpb_sectors_per_cluster}")                        
                        continue
                    cluster_count = bpb_total_sectors / bpb_sectors_per_cluster
                    logging.debug(f"FAT clusters on this disk: {cluster_count}")
                    if cluster_count < 4085:
                        filesystem = "fat12"
                    elif cluster_count < 65525:
                        filesystem = "fat16"
                    else:
                        filesystem = "fat32"
                    break
                else:
                    logging.warning(f"Logical sectors {bpb_total_sectors} indicates this is a {bpb_total_sectors / 2}K floppy")
        else:
            # without the signature we could actually be AtariST, MSX, or
            # linux kernel floppies.  Probably no need to handle these at this
            # time.
            pass
    else:
        format = None
                    
    return format, filesystem

def probe_mac(fe_params, debug):
    "Probe mac formats"
    if fe_params['media_size'] == '5.25':
        # There are no 5.25 macintosh disks
        return None, None    
    
    # check for the 1.44M format floppy thing
    data = read_track("ibm1440", 0, fe_params)
    if has_data(data):
        # test disk: f6's through 0x400.  At 0x400 4244  (that's the HFS Volume signature)
        if get_word(data, 0x400, True) == 0x4244:
            # this is the HFS master file directory signature
            return "ibm1440", "hfs"

    # The only difference between mac400/mac800 is that one is
    # single-sided and the other double sided, but there's no way to
    # tell except through metadata.  Always read it as mac800 and then
    # use the filesystem metadata to determine the actual format.
    data = read_track("mac800", 0, fe_params)
    if not has_data(data):
        logging.debug("No data found from mac800 read")
        return None, None
    if debug:
        dump_data(data, 2048)
    if get_word(data, 0, True) == 0x4c4b:
        # this is an HFS boot block.  HFS was never used on 400K disks as near
        # as I understand.
        return "mac800", "hfs"

    if get_word(data, 0x400, True) == 0x4244:
        # this is an HFS volume 
        return "mac800", "hfs"

    if get_word(data, 0x400, True) == 0xd2d7:
        # The MFS was only used on 400K disks
        return "mac400", "mfs"
    
    return "mac800", None
    
 

def probe_amiga(fe_params, debug):
    if fe_params['media_size'] == '5.25':
        # Not including the relatively rare A1020 drive, Amigas never
        # used the 5.25 format -- and when they did, they'd format
        # the drive as a DOS 360K Disk rather than the 440K Amiga
        # format.
        return None, None
    data = read_track('amiga', 0, fe_params)
    if not has_data(data):        
        logging.debug(f"Amiga Track appears empty.  Skipping")
        return None, None

    if debug:
        dump_data(data, 512)
    fstype = get_string(data, 0, 4)
    fstypes = {"DOS\0": "amiga_ofs",
               "DOS\x01": "amiga_ffs",
               "DOS\x02": "amiga_ofs_international",
               "DOS\x03": "amiga_ffs_international",
               "DOS\x04": "amiga_ofs_international_dircache",
               "DOS\x05": "amiga_ffs_international_dircache"}
    # The amiga encoding is unique enough that if we got any data then
    # it's probably really an amiga disk.  Many games didn't have a
    # valid filesystem, so a filesystem type of None is a legitimate response
    return "amiga", fstypes.get(fstype, None)
    

def probe_c64(fe_params, debug):
    "probe c64 formats"
    if fe_params['media_size'] == '5.25':
        # look for 1541 format, possibly 1571 in the future
        data = read_track("commodore1541", 17, fe_params)
        if has_data(data):
            logging.debug("Looks like c1541 encoding")
            if debug:            
                dump_data(data)
            if data[0x16500] == 0x12 and data[0x16501] == 0x01:
                logging.debug("directory block pointer valid")
                if get_string(data, 0x165a5, 2) == '2A':
                    logging.debug("Disk format correct")
                    return "commodore1541", "2A"
    else:
        # I don't have a sample of 1581.
        pass
    return None, None

def has_data(data):
    "Return true if there is some data here"
    return len([x for x in data if x != 0]) > 0

def get_string(data, offset, length):
    "get an ascii string of length from the data at offset"
    buffer = ""
    for i in range(offset, offset + length):
        buffer += chr(data[i])
    return buffer


def get_word(data, offset, big_endian=False):
    "Get a word in the data at the given offset, optionally decoded big_endian"
    if big_endian:
        return data[offset] * 256 + data[offset + 1]
    else:
        return data[offset] + data[offset + 1] * 256

def get_dword(data, offset, big_endian=False):
    "Get a dword in the data at the given offset, optionally decoded big_endian"
    if big_endian:
        return get_word(data, offset, True) * 65536 + get_word(data, offset + 2, True)
    else:
        return get_word(data, offset) + get_word(data, offset + 2) * 65536

def read_track(decoder, cylinder, fe_params):
    "Build a commandline for fluxengine and read a single track from the drive"
    args = ['fluxengine', 'read', decoder]
    if fe_params['forty_track']:
        args.append('40track_drive')
    tmpfile = tempfile.mktemp() + ".img"
    args.extend([f'-s', f"drive:{fe_params['drive']}",
                 '--output', tmpfile,
                 '--cylinders', str(cylinder),
                 '--decoder.retries=6'])
    logging.debug(f"Fluxengine commandline: {args}")
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf8')
    if p.returncode == 0:
        with open(tmpfile, 'rb') as f:
            data = f.read()
        os.unlink(tmpfile)
    else:
        data = ''
        logging.error(f"Couldn't run {args}: rc={p.returncode}\n{p.stdout}")
    return bytearray(data)

def dump_data(data, limit=None):
    addr = 0
    buffer = ''
    if limit is None:
        limit = len(data)

    for b in data:
        if addr >= limit:
            break
        if addr % 16 == 0:
            print(f"{addr:04x} ", end='')        
        print(f"{b:02x} ", end='')              
        buffer += chr(b) if 32 <= b <= 127 else '.'

        if addr % 16 == 15:
            print(f"  {buffer}")
            buffer = ""
        addr = addr + 1

    if buffer:
        print(f"  {buffer}")


if __name__ == "__main__":
    main()