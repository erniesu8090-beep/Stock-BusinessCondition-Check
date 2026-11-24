"""將單檔儀表板與靜態圖示資源打包為 GitHub Pages 可直接託管的靜態網站目錄。"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
BASE_DIR = SRC_DIR.parent
HTML_PATH = SRC_DIR / "code_artifact.html"
ASSETS_DIR = BASE_DIR / "assets"

MANIFEST_DATA = {
    "name": "台股大戶撤退與籌碼風險判讀儀表板",
    "short_name": "台股籌碼儀表板",
    "description": "整合期現貨籌碼、散戶小微台多空比、融資維持率與觀察名單買點評估。",
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "background_color": "#FAF7F2",
    "theme_color": "#F3EFE6",
    "lang": "zh-Hant",
    "icons": [
        {
            "src": "./assets/app_icon.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable",
        }
    ],
}


def build_static_site(output_dir: Path) -> Path:
    """產出可供 GitHub Pages 部署的靜態網站目錄（含 index.html、PWA manifest 與 assets）。"""
    if not HTML_PATH.exists():
        raise FileNotFoundError(f"找不到前端主檔：{HTML_PATH}")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    html_text = HTML_PATH.read_text(encoding="utf-8")

    # 標記為雲端靜態託管版，讓前端略過本機 /api/* 請求並顯示排程版狀態
    if 'data-static-site=' not in html_text:
        html_text = html_text.replace(
            '<html lang="zh-Hant">',
            '<html lang="zh-Hant" data-static-site="true">',
            1,
        )

    # 調整 assets 相對路徑（由 src/ 移至網站根目錄）並注入 PWA manifest
    html_text = html_text.replace("../assets/", "./assets/")
    if 'rel="manifest"' not in html_text:
        html_text = html_text.replace(
            "</head>",
            '  <link rel="manifest" href="./manifest.webmanifest" />\n</head>',
            1,
        )

    (output_dir / "index.html").write_text(html_text, encoding="utf-8")
    (output_dir / "manifest.webmanifest").write_text(
        json.dumps(MANIFEST_DATA, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")

    if ASSETS_DIR.exists():
        shutil.copytree(ASSETS_DIR, output_dir / "assets", dirs_exist_ok=True)

    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="建置 GitHub Pages 靜態網站產物目錄")
    parser.add_argument(
        "--output",
        type=str,
        default="_site",
        help="輸出目錄（相對專案根目錄或絕對路徑，預設 _site）",
    )
    args = parser.parse_args()

    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = BASE_DIR / out_path

    built = build_static_site(out_path)
    print(f"[PagesBuild] 已完成靜態網站建置：{built}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
