"""Generate reproducible Russian text/table/math fixtures for OCR comparisons."""
from pathlib import Path
import argparse
import zipfile
from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    font_path = Path('C:/Windows/Fonts/arial.ttf')
    if not font_path.exists():
        font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    font = ImageFont.truetype(str(font_path), 34)
    large = ImageFont.truetype(str(font_path), 54)
    small = ImageFont.truetype(str(font_path), 26)
    image = Image.new('RGB', (1250, 850), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((50, 40), 'Расчёт электрического тока', font=large, fill='black')
    draw.text((50, 120), 'Документ СЕВЕР-73. Измерения за 13.10.2026.', font=font, fill='black')
    for y, cells in [(220, ['Параметр', 'Значение', 'Единица']), (290, ['Напряжение U', '12', 'В']), (360, ['Сопротивление R', '4', 'Ом']), (430, ['Ток I', '3', 'А'])]:
        for x, text in zip((65, 620, 950), cells):
            draw.text((x, y), text, font=font, fill='black')
    for y in (205, 275, 345, 415, 485):
        draw.line((50, y, 1200, y), fill='black', width=2)
    for x in (50, 600, 930, 1200):
        draw.line((x, 205, x, 485), fill='black', width=2)
    draw.text((100, 580), 'I =', font=large, fill='black')
    draw.text((270, 540), 'U', font=large, fill='black')
    draw.line((245, 603, 345, 603), fill='black', width=3)
    draw.text((270, 620), 'R', font=large, fill='black')
    draw.text((400, 580), '= 3 А', font=large, fill='black')
    draw.text((50, 750), 'Не путать напряжение (В) и сопротивление (Ом).', font=small, fill='black')
    image.save(args.output / 'russian_table_formula.png')
    # Container-format fixtures share exactly the same visible evidence.
    from docx import Document
    from docx.shared import Inches
    import fitz
    document = Document()
    document.add_heading('Visual ingestion acceptance', level=1)
    document.add_picture(str(args.output / 'russian_table_formula.png'), width=Inches(6))
    document.save(args.output / 'embedded_image.docx')
    pdf = fitz.open()
    page = pdf.new_page(width=625, height=425)
    page.insert_image(page.rect, filename=str(args.output / 'russian_table_formula.png'))
    pdf.save(args.output / 'scanned_image.pdf')
    pdf.close()
    image.save(args.output / 'multipage_image.tiff', save_all=True, append_images=[image.copy()])
    image.crop((70, 520, 650, 715)).save(args.output / 'fraction_formula.png')
    image = Image.new('RGB', (1200, 420), 'white')
    draw = ImageDraw.Draw(image)
    for x, title in ((40, 'Датчик'), (440, 'Контроллер'), (840, 'Сервер')):
        draw.rectangle((x, 120, x + 300, 250), outline='black', width=3)
        draw.text((x + 20, 160), title, font=font, fill='black')
    for x in (340, 740):
        draw.line((x, 185, x + 100, 185), fill='black', width=3)
        draw.polygon([(x + 100, 185), (x + 85, 177), (x + 85, 193)], fill='black')
    draw.text((350, 275), 'RS-485', font=small, fill='black')
    draw.text((750, 275), 'Ethernet', font=small, fill='black')
    image.save(args.output / 'diagram.png')
    for path in Path('output/diagnostics/2026-10-12/production').glob('*1179*.docx'):
        with zipfile.ZipFile(path) as archive:
            for name in ('word/media/image44.jpeg', 'word/media/image51.jpeg'):
                if name in archive.namelist():
                    (args.output / Path(name).name).write_bytes(archive.read(name))
    print(args.output)


if __name__ == '__main__':
    main()
