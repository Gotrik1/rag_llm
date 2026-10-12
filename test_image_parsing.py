import io
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import fitz
from PIL import Image
import image_parsing


def png():
    buffer = io.BytesIO()
    Image.new('RGB', (600, 400), 'white').save(buffer, 'PNG')
    return buffer.getvalue()


def response(text, finish='stop'):
    result = Mock()
    result.json.return_value = {'choices': [{'message': {'content': text}, 'finish_reason': finish}]}
    return result


def test_visual_text_tables_formulas_are_cached_by_content_and_model():
    text = '| U | R |\n| 12 | 4 |\n\n$$I=\\frac{U}{R}$$'
    with tempfile.TemporaryDirectory() as directory, \
         patch.object(image_parsing, 'INGESTION_CACHE_DIR', Path(directory)), \
         patch.dict('os.environ', {'RAG_VISION_BASE_URL': 'http://vision.invalid/v1', 'RAG_VISION_MODEL': 'vision-1'}), \
         patch('image_parsing.requests.post', return_value=response(text)) as post:
        first = image_parsing.parse_image_bytes(png(), 'one.png')
        second = image_parsing.parse_image_bytes(png(), 'other-name.png')
        assert first.text == second.text == text
        assert first.engine == 'qwen-vision'
        assert post.call_count == 1
        with patch.dict('os.environ', {'RAG_VISION_MODEL': 'vision-2'}):
            image_parsing.parse_image_bytes(png(), 'one.png')
        assert post.call_count == 2
        payload = post.call_args.kwargs['json']
        assert payload['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')


def test_truncated_visual_response_is_not_cached_as_complete():
    with tempfile.TemporaryDirectory() as directory, \
         patch.object(image_parsing, 'INGESTION_CACHE_DIR', Path(directory)), \
         patch.dict('os.environ', {'RAG_VISION_BASE_URL': 'http://vision.invalid/v1'}), \
         patch('image_parsing.requests.post', return_value=response('partial', 'length')) as post, \
         patch('pytesseract.image_to_string', return_value='Fallback text'):
        first = image_parsing.parse_image_bytes(png(), 'image.png')
        assert first.engine == 'tesseract-ocr' and first.warnings
        image_parsing.parse_image_bytes(png(), 'image.png')
        assert post.call_count == 2


def test_specialized_formula_is_kept_as_math():
    result = Mock()
    result.json.return_value = {'text': r'\[I=\frac{U}{R}\]'}
    with tempfile.TemporaryDirectory() as directory, \
         patch.object(image_parsing, 'INGESTION_CACHE_DIR', Path(directory)), \
         patch.dict('os.environ', {'RAG_FORMULA_OCR_URL': 'http://formula.invalid/parse'}), \
         patch('image_parsing.requests.post', return_value=result):
        parsed = image_parsing.parse_image_bytes(png(), 'formula.png', 'formula')
        assert parsed.text == '$$\nI=\\frac{U}{R}\n$$'
        assert parsed.engine == 'paddleocr-vl-1.6'
        assert parsed.warnings


def test_scan_pdf_uses_visual_content_with_page_locator():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'scan.pdf'
        with fitz.open() as document:
            document.new_page().insert_image(fitz.Rect(0, 0, 600, 400), stream=png())
            document.save(path)
        with patch.dict('os.environ', {'RAG_VISION_BASE_URL': 'http://vision.invalid/v1'}), \
             patch('image_parsing.parse_image_bytes', return_value=image_parsing.ParsedImage('СЕВЕР-73', 'qwen-vision', [])) as parse:
            result = image_parsing.read_pdf(path)
        assert '[SECTION: Страница 1]' in result.text and 'СЕВЕР-73' in result.text
        assert parse.call_count == 1


def test_native_pdf_text_does_not_require_ocr():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'native.pdf'
        with fitz.open() as document:
            document.new_page().insert_text((40, 40), 'Project ALPHA-42. This is a native PDF paragraph with readable text.')
            document.save(path)
        with patch.dict('os.environ', {'RAG_VISION_BASE_URL': ''}), \
             patch('image_parsing.parse_image_bytes') as parse:
            result = image_parsing.read_pdf(path)
        assert 'ALPHA-42' in result.text and result.engine == 'pdf-native'
        parse.assert_not_called()


def test_multiframe_tiff_keeps_every_page():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'scan.tiff'
        Image.new('RGB', (200, 200), 'white').save(path, save_all=True, append_images=[Image.new('RGB', (200, 200), 'black')])
        with patch('image_parsing.parse_image_bytes', side_effect=[image_parsing.ParsedImage('ONE', 'vision', []), image_parsing.ParsedImage('TWO', 'vision', [])]):
            result = image_parsing.parse_image_file(path)
        assert '[SECTION: Страница 1]' in result.text and '[SECTION: Страница 2]' in result.text
        assert 'ONE' in result.text and 'TWO' in result.text


def test_long_screenshot_retains_bottom_content_without_downscaling_all_text():
    buffer = io.BytesIO()
    Image.new('RGB', (600, 5000), 'white').save(buffer, 'PNG')
    with patch('image_parsing._recognize', side_effect=[image_parsing.ParsedImage('TOP', 'vision', []), image_parsing.ParsedImage('MIDDLE', 'vision', []), image_parsing.ParsedImage('BOTTOM', 'vision', [])]) as recognize, \
         tempfile.TemporaryDirectory() as directory, patch.object(image_parsing, 'INGESTION_CACHE_DIR', Path(directory)):
        # Distinct crops are needed to exercise all three positions.
        image = Image.open(io.BytesIO(buffer.getvalue()))
        for y in (100, 2400, 4700):
            image.putpixel((10, y), (0, 0, 0))
        input_buffer = io.BytesIO(); image.save(input_buffer, 'PNG')
        parsed = image_parsing.parse_image_bytes(input_buffer.getvalue(), 'long.png')
        assert 'BOTTOM' in parsed.text and '[SECTION: Изображение > Часть 3]' in parsed.text
        assert recognize.call_count == 3
        assert all(call.args[0].width == 600 for call in recognize.call_args_list)
