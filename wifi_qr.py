#!/usr/bin/env python3
"""Генерация QR-кода для подключения к Wi-Fi с подписью (SSID и пароль) под ним.

Пример:
    python3 wifi_qr.py --ssid "Guest-WiFi" --password "Mi4tyW1nd0wSunset42" -o guest-wifi-qr.png

Зависимость:
    pip3 install --user "qrcode[pil]"
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    import qrcode
    from PIL import Image, ImageDraw, ImageFont
    from qrcode.constants import ERROR_CORRECT_M
except ImportError:
    sys.exit(
        "Не найдены зависимости qrcode/Pillow.\n"
        "Установите их командой:  pip3 install --user 'qrcode[pil]'"
    )

# Символы, которые в формате WIFI: должны быть экранированы обратным слешем.
_WIFI_SPECIAL = '\\;,:/"'

# Где искать TrueType-шрифт (первый найденный используется для подписи).
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


def escape_wifi(value: str) -> str:
    """Экранирует спецсимволы согласно спецификации Wi-Fi QR-кодов."""
    return "".join(("\\" + ch) if ch in _WIFI_SPECIAL else ch for ch in value)


def load_font(size: int) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        # Pillow >= 10.1 умеет масштабировать встроенный шрифт.
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def build_payload(ssid: str, password: str) -> str:
    return f"WIFI:T:WPA;S:{escape_wifi(ssid)};P:{escape_wifi(password)};;"


def render(ssid: str, password: str, scale: int, gap: int) -> tuple[Image.Image, str]:
    payload = build_payload(ssid, password)

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=scale, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_w, qr_h = qr_img.size

    lines = [f"Wi-Fi: {ssid}", f"Пароль: {password}"]

    padding = max(40, qr_w // 12)
    canvas_w = qr_w + 2 * padding
    available_w = qr_w

    # Подбор размера шрифта: хотим крупно, но чтобы всё влезло по ширине.
    tmp_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    font_size = max(20, qr_w // 12)
    font = load_font(font_size)
    while font_size > 12 and max(
        tmp_draw.textlength(line, font=font) for line in lines
    ) > available_w:
        font_size -= 1
        font = load_font(font_size)

    line_h = font_size + 12
    text_block_h = len(lines) * line_h
    canvas_h = padding + qr_h + gap + text_block_h + padding

    canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
    canvas.paste(qr_img, ((canvas_w - qr_w) // 2, padding))

    draw = ImageDraw.Draw(canvas)
    y = padding + qr_h + gap
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((canvas_w - w) / 2, y), line, fill="black", font=font)
        y += line_h

    return canvas, payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Сгенерировать QR-код Wi-Fi с названием сети и паролем под ним.",
    )
    parser.add_argument("--ssid", required=True, help="Название (SSID) Wi-Fi сети")
    parser.add_argument("--password", required=True, help="Пароль Wi-Fi сети")
    parser.add_argument(
        "-o",
        "--output",
        default="wifi-qr.png",
        help="Куда сохранить PNG (по умолчанию: wifi-qr.png)",
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=12,
        help="Размер одного модуля QR в пикселях, больше = крупнее для печати (по умолчанию: 12)",
    )
    parser.add_argument(
        "--gap",
        type=int,
        default=40,
        help="Отступ между QR-кодом и подписью в пикселях (по умолчанию: 40)",
    )
    args = parser.parse_args()

    if len(args.ssid) == 0:
        parser.error("SSID не может быть пустым")
    if len(args.password) < 8:
        print(
            "Внимание: пароль короче 8 символов — WPA2-PSK его не примет.",
            file=sys.stderr,
        )
    elif len(args.password) < 14:
        print(
            "Совет: для гостевой сети лучше пароль длиной 14+ символов.",
            file=sys.stderr,
        )

    image, payload = render(args.ssid, args.password, args.scale, args.gap)
    image.save(args.output, dpi=(300, 300))

    print(f"Строка подключения: {payload}")
    print(f"Файл сохранён:      {os.path.abspath(args.output)}")
    print(f"Размер изображения: {image.width}x{image.height} px (300 dpi для печати)")


if __name__ == "__main__":
    main()
