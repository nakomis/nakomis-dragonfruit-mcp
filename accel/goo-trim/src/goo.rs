//! The `.goo` container: header checks and the layer table.
//!
//! Mirrors `goo.read_header` and `goo_motion._layer_definitions` in the Python:
//! the same layout (DragonFruit's GOO V1.2 writer, big-endian) and the same
//! refusals, so a file one side rejects the other rejects too.

pub const FILE_MAGIC: [u8; 8] = [0x07, 0x00, 0x00, 0x00, 0x44, 0x4C, 0x50, 0x00];
/// 194 bytes of identity strings, then two RGB565 previews (116x116, 290x290), each + CRLF.
pub const SETTINGS_OFFSET: usize = 194 + (116 * 116 * 2 + 2) + (290 * 290 * 2 + 2);
pub const HEADER_BYTES: usize = SETTINGS_OFFSET + 176;
pub const LAYER_DEF_BYTES: usize = 66;
/// 66-byte definition, u32 size, 0x55 + checksum, CRLF.
pub const MIN_LAYER_BYTES: usize = LAYER_DEF_BYTES + 4 + 2 + 2;
pub const MAX_SIDE_PX: usize = 30_000;
pub const MAX_PIXELS: usize = 400_000_000;
/// Both DragonFruit and Chitubox end a .goo with this after the last layer.
pub const END_MARKER: [u8; 11] = [0, 0, 0, 7, 0, 0, 0, b'D', b'L', b'P', 0];

/// One layer: where its 66-byte definition starts, and its RLE data.
#[derive(Clone, Copy, Debug)]
pub struct Layer {
    pub def_off: usize,
    pub data_off: usize,
    pub size: usize,
}

impl Layer {
    /// The whole record: definition, size, data and CRLF.
    pub fn record(&self) -> std::ops::Range<usize> {
        self.def_off..self.data_off + self.size + 2
    }
    pub fn data<'a>(&self, file: &'a [u8]) -> &'a [u8] {
        &file[self.data_off..self.data_off + self.size]
    }
}

#[derive(Debug)]
pub struct Goo {
    pub width: usize,
    pub height: usize,
    pub layers: Vec<Layer>,
    pub ends_with_marker: bool,
}

fn u32_at(data: &[u8], at: usize) -> usize {
    u32::from_be_bytes([data[at], data[at + 1], data[at + 2], data[at + 3]]) as usize
}

fn u16_at(data: &[u8], at: usize) -> usize {
    u16::from_be_bytes([data[at], data[at + 1]]) as usize
}

pub fn is_goo(head: &[u8]) -> bool {
    head.len() >= 12 && (&head[..4] == b"V1.2" || &head[..4] == b"V3.0") && head[4..12] == FILE_MAGIC
}

/// Check the header and walk the whole layer table; nothing is decoded.
pub fn parse(data: &[u8]) -> Result<Goo, String> {
    if !is_goo(data) {
        return Err("not a GOO V1.2/V3.0 file".into());
    }
    if data.len() < HEADER_BYTES {
        return Err("truncated inside the header".into());
    }
    let s = SETTINGS_OFFSET;
    let layers = u32_at(data, s);
    let width = u16_at(data, s + 4);
    let height = u16_at(data, s + 6);
    let table = u32_at(data, s + 160);
    if layers == 0 || width == 0 || height == 0 {
        return Err("the header does not make sense (no layers or resolution)".into());
    }
    if width > MAX_SIDE_PX || height > MAX_SIDE_PX || width * height > MAX_PIXELS {
        return Err(format!(
            "implausible screen {width}x{height} (limits {MAX_SIDE_PX} per side, {} MP)",
            MAX_PIXELS / 1_000_000
        ));
    }
    if table < s + 164 || table > data.len() {
        return Err(format!("layer table offset {table} is outside the file"));
    }
    if layers.saturating_mul(MIN_LAYER_BYTES) > data.len() - table {
        return Err(format!("the header claims {layers} layers but the file is too short"));
    }
    let mut out = Vec::with_capacity(layers);
    let mut off = table;
    for index in 1..=layers {
        let end_of_def = off + LAYER_DEF_BYTES;
        if end_of_def + 4 > data.len() || &data[end_of_def - 2..end_of_def] != b"\r\n" {
            return Err(format!("layer {index} is not where expected"));
        }
        let size = u32_at(data, end_of_def);
        let end = end_of_def + 4 + size;
        if end + 2 > data.len() || &data[end..end + 2] != b"\r\n" {
            return Err(format!("layer {index} is truncated"));
        }
        out.push(Layer { def_off: off, data_off: end_of_def + 4, size });
        off = end + 2;
    }
    let rest = &data[off..];
    if !(rest.is_empty() || rest == END_MARKER) {
        return Err(format!("{} unexpected bytes after the last layer", rest.len()));
    }
    Ok(Goo { width, height, layers: out, ends_with_marker: !rest.is_empty() })
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use crate::rle;

    /// A whole .goo, the same as the Python tests' `make_goo` (plus an optional end marker).
    pub fn make_goo(layers: &[Vec<u8>], width: usize, height: usize, marker: bool) -> Vec<u8> {
        let s = SETTINGS_OFFSET;
        let mut head = vec![0u8; HEADER_BYTES];
        head[..4].copy_from_slice(b"V1.2");
        head[4..12].copy_from_slice(&FILE_MAGIC);
        head[s..s + 4].copy_from_slice(&(layers.len() as u32).to_be_bytes());
        head[s + 4..s + 6].copy_from_slice(&(width as u16).to_be_bytes());
        head[s + 6..s + 8].copy_from_slice(&(height as u16).to_be_bytes());
        head[s + 22..s + 26].copy_from_slice(&0.05f32.to_be_bytes());
        head[s + 160..s + 164].copy_from_slice(&(HEADER_BYTES as u32).to_be_bytes());
        let mut out = head;
        for pixels in layers {
            let data = rle::encode_pixels(pixels);
            out.extend_from_slice(&[0u8; 64]);
            out.extend_from_slice(b"\r\n");
            out.extend_from_slice(&(data.len() as u32).to_be_bytes());
            out.extend_from_slice(&data);
            out.extend_from_slice(b"\r\n");
        }
        if marker {
            out.extend_from_slice(&END_MARKER);
        }
        out
    }

    #[test]
    fn parses_a_small_file() {
        let g = parse(&make_goo(&[vec![0; 8], vec![255; 8]], 4, 2, true)).unwrap();
        assert_eq!((g.width, g.height, g.layers.len(), g.ends_with_marker), (4, 2, 2, true));
    }

    #[test]
    fn refuses_trailing_junk_and_truncation() {
        let mut f = make_goo(&[vec![0; 8]], 4, 2, false);
        f.push(1);
        assert!(parse(&f).unwrap_err().contains("unexpected bytes"));
        let f = make_goo(&[vec![0; 8]], 4, 2, false);
        assert!(parse(&f[..f.len() - 1]).is_err());
        assert!(parse(b"nonsense").is_err());
    }
}
