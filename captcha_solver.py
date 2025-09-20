from PIL import Image, ImageOps, ImageFilter
import pytesseract

def solve_captcha(image_path):
    try:
        img = Image.open(image_path).convert("L")
        img = img.filter(ImageFilter.MedianFilter())  # remove noise
        img = img.point(lambda x: 0 if x < 150 else 255, '1')  # binarize
        img = img.resize((img.width * 3, img.height * 3))  # upscale

        configs = [
            "--psm 7 --oem 3 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
            "--psm 8 --oem 3 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
            "--psm 6 --oem 3 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        ]

        for cfg in configs:
            text = pytesseract.image_to_string(img, config=cfg).strip()
            if text and len(text) >= 4:
                return text.upper()  # normalize to uppercase

        return ""
    except Exception as e:
        print("❌ OCR error:", e)
        return ""
