use anyhow::{bail, Result};
use std::path::Path;

pub struct Chunk {
    pub text: String,
    pub source_file: String,
    pub chunk_id: usize,
    pub section: String,
}

const CHUNK_SIZE: usize = 1500;
const CHUNK_OVERLAP: usize = 200;

pub fn parse_and_chunk(path: &str) -> Result<Vec<Chunk>> {
    let p = Path::new(path);
    let ext = p.extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_lowercase();

    let sections = match ext.as_str() {
        "md" | "txt" => parse_markdown(path)?,
        "pdf"        => parse_pdf(path)?,
        "docx"       => parse_docx_by_styles(path)?,
        other        => bail!("Неподдерживаемый формат: .{}", other),
    };

    Ok(sections_to_chunks(sections, path))
}

// ─────────────────────────────────────────────
// DOCX: читаем стили параграфов из XML
// ─────────────────────────────────────────────
fn parse_docx_by_styles(path: &str) -> Result<Vec<(String, String)>> {
    use std::io::Read;

    let file = std::fs::File::open(path)?;
    let mut archive = zip::ZipArchive::new(file)?;

    let mut xml = String::new();
    {
        let mut entry = archive.by_name("word/document.xml")?;
        entry.read_to_string(&mut xml)?;
    }

    // Парсим параграфы вручную через стейт-машину по тегам
    let mut sections: Vec<(String, String)> = Vec::new();
    let mut current_heading = String::new();
    let mut current_body   = String::new();

    // Обходим XML побайтно, извлекаем параграфы с их стилем
    for para in split_paragraphs(&xml) {
        let style_level = detect_heading_level(&para);
        let text = extract_para_text(&para);
        let text = text.trim().to_string();

        if text.is_empty() {
            continue;
        }

        if style_level.is_some() {
            // Это заголовок — сохраняем предыдущий раздел
            if !current_body.trim().is_empty() {
                sections.push((current_heading.clone(), current_body.clone()));
            }
            current_heading = text;
            current_body = String::new();
        } else {
            current_body.push_str(&text);
            current_body.push('\n');
        }
    }

    if !current_body.trim().is_empty() {
        sections.push((current_heading, current_body));
    }

    Ok(sections)
}

/// Разбивает XML документа на параграфы `<w:p>...</w:p>`
fn split_paragraphs(xml: &str) -> Vec<String> {
    let mut paras = Vec::new();
    let mut rest = xml;
    while let Some(start) = rest.find("<w:p ").or_else(|| rest.find("<w:p>")) {
        rest = &rest[start..];
        if let Some(end) = rest.find("</w:p>") {
            paras.push(rest[..end + 6].to_string());
            rest = &rest[end + 6..];
        } else {
            break;
        }
    }
    paras
}

/// Определяет уровень заголовка по стилю параграфа.
/// Ищет <w:pStyle w:val="Heading1"/>, "1", "2", "heading1", etc.
fn detect_heading_level(para_xml: &str) -> Option<u32> {
    // Ищем <w:pStyle w:val="..."/>
    if let Some(pos) = para_xml.find("w:pStyle") {
        if let Some(val_pos) = para_xml[pos..].find("w:val=\"") {
            let val_start = pos + val_pos + 7;
            if let Some(val_end) = para_xml[val_start..].find('"') {
                let val = &para_xml[val_start..val_start + val_end];
                let val_lower = val.to_lowercase();
                // "Heading1", "heading 1", "1", "2", "3", "Heading 2" и т.д.
                if val_lower.starts_with("heading") || val_lower.starts_with("заголовок") {
                    let level_str = val_lower
                        .chars()
                        .filter(|c| c.is_ascii_digit())
                        .next()
                        .map(|c| c.to_digit(10).unwrap_or(1))
                        .unwrap_or(1);
                    return Some(level_str);
                }
                // Некоторые DOCX используют просто "1", "2", "3" как стиль заголовка
                if let Ok(n) = val.parse::<u32>() {
                    if n >= 1 && n <= 6 {
                        return Some(n);
                    }
                }
            }
        }
    }
    None
}

/// Извлекает текст из параграфа XML (только `<w:t>` теги)
fn extract_para_text(para_xml: &str) -> String {
    let mut text = String::new();
    let mut rest = para_xml;
    while let Some(start) = rest.find("<w:t") {
        rest = &rest[start..];
        // Пропускаем до конца открывающего тега
        if let Some(tag_end) = rest.find('>') {
            rest = &rest[tag_end + 1..];
            if let Some(close) = rest.find("</w:t>") {
                text.push_str(&rest[..close]);
                rest = &rest[close + 6..];
            } else {
                break;
            }
        } else {
            break;
        }
    }
    text
}

// ─────────────────────────────────────────────
// PDF
// ─────────────────────────────────────────────
fn parse_pdf(path: &str) -> Result<Vec<(String, String)>> {
    use lopdf::Document;
    let doc = Document::load(path)?;
    let mut full_text = String::new();
    for page_id in doc.page_iter() {
        if let Ok(content) = doc.extract_text(&[page_id.0]) {
            full_text.push_str(&content);
            full_text.push('\n');
        }
    }
    // PDF — просто режем по тексту, без стилей
    Ok(split_by_text_patterns(&full_text))
}

// ─────────────────────────────────────────────
// Markdown / TXT
// ─────────────────────────────────────────────
fn parse_markdown(path: &str) -> Result<Vec<(String, String)>> {
    let text = std::fs::read_to_string(path)?;
    Ok(split_by_text_patterns(&text))
}

/// Фолбэк-сплиттер для PDF/TXT: ищет паттерны "2.2.1." в начале строки
fn split_by_text_patterns(text: &str) -> Vec<(String, String)> {
    let mut sections: Vec<(String, String)> = Vec::new();
    let mut current_header = String::new();
    let mut current_body   = String::new();

    for line in text.lines() {
        let trimmed = line.trim();
        if is_numbered_header(trimmed) {
            if !current_body.trim().is_empty() {
                sections.push((current_header.clone(), current_body.clone()));
            }
            current_header = trimmed.to_string();
            current_body = String::new();
        } else {
            current_body.push_str(line);
            current_body.push('\n');
        }
    }
    if !current_body.trim().is_empty() {
        sections.push((current_header, current_body));
    }
    if sections.is_empty() {
        sections.push((String::new(), text.to_string()));
    }
    sections
}

fn is_numbered_header(line: &str) -> bool {
    if line.is_empty() || line.len() > 80 {
        return false;
    }
    // "1.", "2.1.", "2.2.6." с текстом после
    let mut chars = line.chars().peekable();
    let first = chars.next();
    if !first.map(|c| c.is_ascii_digit()).unwrap_or(false) {
        return false;
    }
    let has_dot = line[1..].contains('.');
    let has_text = line.chars().any(|c| c.is_alphabetic());
    has_dot && has_text
}

// ─────────────────────────────────────────────
// Нарезка секций на чанки
// ─────────────────────────────────────────────
fn sections_to_chunks(sections: Vec<(String, String)>, source: &str) -> Vec<Chunk> {
    let mut chunks = Vec::new();
    let mut chunk_id = 0;

    for (header, body) in sections {
        let body = body.trim();
        if body.is_empty() {
            continue;
        }

        // Каждый чанк начинается с заголовка раздела — это критично для BM25 и vector search
        let full = if header.is_empty() {
            body.to_string()
        } else {
            format!("{}\n{}", header, body)
        };

        let char_count = full.chars().count();
        if char_count <= CHUNK_SIZE {
            chunks.push(Chunk {
                text: full,
                source_file: source.to_string(),
                chunk_id,
                section: header.clone(),
            });
            chunk_id += 1;
        } else {
            // Длинный раздел — режем с overlap, каждый под-чанк сохраняет заголовок
            let chars: Vec<char> = full.chars().collect();
            let mut start = 0;
            while start < chars.len() {
                let end = (start + CHUNK_SIZE).min(chars.len());
                let sub: String = chars[start..end].iter().collect();
                let sub = sub.trim();
                if !sub.is_empty() {
                    let sub_text = if start > 0 && !header.is_empty() {
                        format!("[{}] (продолжение)\n{}", header, sub)
                    } else {
                        sub.to_string()
                    };
                    chunks.push(Chunk {
                        text: sub_text,
                        source_file: source.to_string(),
                        chunk_id,
                        section: header.clone(),
                    });
                    chunk_id += 1;
                }
                if end == chars.len() { break; }
                start += CHUNK_SIZE - CHUNK_OVERLAP;
            }
        }
    }

    chunks
}
