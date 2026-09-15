from PIL import Image
import os

def convert_png_to_ico(png_path, ico_path):
    if not os.path.exists(png_path):
        print(f"Error: {png_path} not found.")
        return

    img = Image.open(png_path).convert("RGBA")
    # Windows ICO 建议包含这些尺寸
    icon_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    frames = [img.resize(size, Image.Resampling.LANCZOS) for size in icon_sizes]
    # 使用 BMP 条目：Inno Setup / 部分 GDI API 对纯 PNG 压缩 ICO 会静默回退默认图标
    frames[-1].save(
        ico_path,
        format="ICO",
        sizes=icon_sizes,
        append_images=frames[:-1],
        bitmap_format="bmp",
    )
    print(f"Success: {ico_path} generated.")

if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.abspath(__file__))
    convert_png_to_ico(
        os.path.join(base_dir, "mibuddy.png"),
        os.path.join(base_dir, "mibuddy.ico")
    )
