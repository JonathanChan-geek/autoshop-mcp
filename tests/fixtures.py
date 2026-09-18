"""Synthetic parser fixture only; never download or compile this as a PLC project."""
from autoshop_mcp import hcp, il

def create_project(root):
    root.mkdir()
    xml = '<?xml version="1.0" encoding="UTF-16"?><project><GCMModal>H3U</GCMModal><HardwareFile>H3U.dll</HardwareFile><file id="0"><FileName>MAIN.LD</FileName><FileType>1</FileType><ProgType>0</ProgType><Encrypted>0</Encrypted></file><file id="1"><FileName>DEMO.IL</FileName><FileType>0</FileType><ProgType>1</ProgType><Encrypted>0</Encrypted></file></project>'
    raw = xml.encode('utf-16')
    encoded = bytes(((v+hcp.HCP_KEY[(i+1)%11])&255) ^ (i&255 if i%2==0 else 0) for i,v in enumerate(raw))
    (root/'demo.hcp').write_bytes(encoded)
    header = bytearray(il.HEADER_SIZE)
    header[:8] = il.MAGIC
    header[143:151] = il.HEADER_MARKER
    (root/'DEMO.IL').write_bytes(il.render('LD\t\t X0\nOUT\t\t Y1\nLD\t\t X1\nOUT\t\t Y3\n', bytes(header), b'\0'*8))
    (root/'MAIN.LD').write_bytes(b'synthetic opaque ladder placeholder')
    for name in ['MAIN.dat','MAIN.mon','CANLink.prg']:
        (root/name).write_bytes(('synthetic preserved '+name).encode())
    (root/'Output.prg').write_bytes(b'synthetic stale output')
