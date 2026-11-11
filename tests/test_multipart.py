import os
import sys
from email.parser import BytesParser
from email import policy

raw = (
    b"Content-Type: multipart/form-data; boundary=xyz\r\n\r\n"
    b"--xyz\r\n"
    b"Content-Disposition: form-data; name=\"user\"\r\n\r\n"
    b"Alice\r\n"
    b"--xyz\r\n"
    b"Content-Disposition: form-data; name=\"file\"; filename=\"doc.pdf\"\r\n\r\n"
    b"FAKEPDFDATA\r\n"
    b"--xyz--"
)

msg = BytesParser(policy=policy.default).parsebytes(raw)
for part in msg.iter_parts():
    print("name:", part.get_param("name", header="content-disposition"))
    print("filename:", part.get_filename())
    print("payload:", part.get_payload(decode=True))
