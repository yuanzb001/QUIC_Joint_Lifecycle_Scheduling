import json
import subprocess
from pathlib import Path

VTM_BIN = Path("/home/kopn/VVCSoftware_VTM/bin")
EXTRACTOR = VTM_BIN / "BitstreamExtractorAppStatic"

NAL_NAMES = {
    0:  "TRAIL", 1:  "STSA", 2:  "RADL", 3:  "RASL", 7:  "IDR_W_RADL",
    8:  "IDR_N_LP", 9:  "CRA", 10: "GDR", 12: "OPI", 13: "DCI", 14: "VPS",
    15: "SPS", 16: "PPS", 17: "PREFIX_APS", 18: "SUFFIX_APS", 19: "PH",
    20: "AUD", 21: "EOS", 22: "EOB", 23: "PREFIX_SEI", 24: "SUFFIX_SEI", 25: "FD",
}

def find_start_codes(data):
    result = []
    i = 0
    while i < len(data) - 3:
        if data[i:i+4] == b"\x00\x00\x00\x01":
            result.append((i, 4))
            i += 4
            continue
        if data[i:i+3] == b"\x00\x00\x01":
            result.append((i, 3))
            i += 3
            continue
        i += 1
    return result

def parse_annexb(path):
    data = Path(path).read_bytes()
    starts = find_start_codes(data)
    if not starts:
        raise RuntimeError(f"No Annex-B start code found in {path}")
    nalus = []
    for i, (start, sc_len) in enumerate(starts):
        if i + 1 < len(starts):
            end = starts[i + 1][0]
        else:
            end = len(data)
        raw = data[start:end]
        payload_start = start + sc_len
        if payload_start + 2 > end:
            continue
        header = data[payload_start:payload_start + 2]
        nal_type = (header[1] >> 3) & 0x1F
        nalus.append({
            "index": len(nalus),
            "type": nal_type,
            "name": NAL_NAMES.get(nal_type, f"NAL_{nal_type}"),
            "raw": raw,
        })
    return nalus

def run_extractor(input_vvc, output_vvc, subpic_idx):
    cmd = [
        str(EXTRACTOR),
        "-b", str(input_vvc),
        "-o", str(output_vvc),
        f"--SubPicIdx={subpic_idx}",
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"BitstreamExtractorApp failed for SubPicIdx={subpic_idx}")

def get_vcl_nalus(nalus):
    return [n for n in nalus if 0 <= n["type"] <= 11]

def nal_payload_without_start_code(raw):
    if raw.startswith(b"\x00\x00\x00\x01"):
        return raw[4:]
    if raw.startswith(b"\x00\x00\x01"):
        return raw[3:]
    return raw

def split(input_vvc, output_dir, num_subpics=2):
    """
    Splits a VVC bitstream into common headers and subpicture bitstreams.
    Returns a dictionary of generated files.
    """
    input_vvc = Path(input_vvc)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"[Splitter] Parsing original VVC: {input_vvc}")
    original_nalus = parse_annexb(input_vvc)
    subpic_payload_sets = []
    
    for sid in range(num_subpics):
        print(f"[Splitter] Extracting subpicture {sid} using BitstreamExtractorApp...")
        tmp_file = output_dir / f"_extractor_subpic_{sid}.vvc"
        run_extractor(input_vvc, tmp_file, sid)
        extracted_nalus = parse_annexb(tmp_file)
        vcl = get_vcl_nalus(extracted_nalus)
        payloads = set()
        for n in vcl:
            payloads.add(nal_payload_without_start_code(n["raw"]))
        subpic_payload_sets.append(payloads)
        
    manifest = []
    common_file = output_dir / "common.vvc"
    subpic_files = [output_dir / f"subpic_{sid}.vvc" for sid in range(num_subpics)]
    
    common_fp = open(common_file, "wb")
    subpic_fps = [open(p, "wb") for p in subpic_files]
    common_idx = 0
    subpic_idx = [0] * num_subpics
    
    print("[Splitter] Generating common.vvc and individual subpicture VVCs...")
    try:
        for n in original_nalus:
            raw = n["raw"]
            if not (0 <= n["type"] <= 11): # Non-VCL goes to Common
                common_fp.write(raw)
                manifest.append({
                    "source": "common",
                    "local_index": common_idx,
                    "nal_type": n["type"],
                    "nal_name": n["name"],
                    "size": len(raw),
                })
                common_idx += 1
                continue
                
            payload = nal_payload_without_start_code(raw)
            owner = None
            for sid in range(num_subpics):
                if payload in subpic_payload_sets[sid]:
                    owner = sid
                    break
                    
            if owner is None:
                raise RuntimeError(f"Could not determine subpicture for NAL #{n['index']} type={n['type']}.")
                
            subpic_fps[owner].write(raw)
            manifest.append({
                "source": f"subpic_{owner}",
                "local_index": subpic_idx[owner],
                "nal_type": n["type"],
                "nal_name": n["name"],
                "size": len(raw),
            })
            subpic_idx[owner] += 1
    finally:
        common_fp.close()
        for fp in subpic_fps:
            fp.close()
            
    manifest_path = output_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
        
    print(f"[Splitter] Split complete! Saved to {output_dir}")
    return {
        "common": str(common_file),
        "subpics": [str(p) for p in subpic_files],
        "manifest": str(manifest_path)
    }

def merge(input_dir, output_vvc, num_subpics=2):
    """
    Merges split subpicture bitstreams back into a single playable VVC file.
    """
    input_dir = Path(input_dir)
    manifest_path = input_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    
    print(f"[Splitter] Merging from {input_dir} into {output_vvc}...")
    streams = {}
    streams["common"] = parse_annexb(input_dir / "common.vvc")
    for sid in range(num_subpics):
        streams[f"subpic_{sid}"] = parse_annexb(input_dir / f"subpic_{sid}.vvc")
        
    output_vvc = Path(output_vvc)
    with open(output_vvc, "wb") as out:
        for item in manifest:
            source = item["source"]
            local_index = item["local_index"]
            nalu = streams[source][local_index]
            out.write(nalu["raw"])
            
    print("[Splitter] Merge complete!")
