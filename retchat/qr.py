"""Creating and reading QR codes (no GTK; see widgets.qr_code_view / qr_scanner)."""

from typing import List

QUIET_ZONE = 4  # modules of white border required by the QR standard


def qr_matrix(text: str) -> List[List[bool]]:
    """Modules of a QR code for ``text`` (True = dark), quiet zone included."""
    import qrcode
    # Medium error correction: still a small code for an lxma:// link
    # (version 9), and readable from a phone screen.
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=QUIET_ZONE)
    qr.add_data(text)
    qr.make(fit=True)
    return [[bool(cell) for cell in row] for row in qr.get_matrix()]


def decode_gray(data: bytes, width: int, height: int, stride: int) -> List[str]:
    """Texts of all QR codes in an 8-bit grayscale frame (rows ``stride`` bytes apart)."""
    import zxingcpp
    if stride < width or len(data) < stride * height:
        return []
    image = memoryview(data)[:stride * height].cast("B", (height, stride))
    return [r.text for r in zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.QRCode) if r.text]


def decode_image_file(path: str) -> List[str]:
    """Texts of all QR codes in an image file (photo, screenshot)."""
    import zxingcpp
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("L")
        return [r.text for r in zxingcpp.read_barcodes(im, formats=zxingcpp.BarcodeFormat.QRCode) if r.text]
