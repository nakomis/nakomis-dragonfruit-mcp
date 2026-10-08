//! A layer's RLE image: `0x55`, runs, then a checksum byte (the bitwise NOT of
//! the sum of the run bytes). A run starts with `[TT][SS][CCCC]`: TT 00 black,
//! 01 grey (the grey value is the next byte), 11 white; SS says how many extra
//! length bytes follow (0-3, big-endian, above the 4 low bits in CCCC).
//! DragonFruit's V1.2 encoder never writes step runs (TT 10), so they are refused.
//!
//! The decoder refuses exactly what the Python's `_decode_slowly` refuses; the
//! encoder writes what `goo.encode_layer` writes, byte for byte.

/// A run of `len` pixels of grey `value`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Run {
    pub value: u8,
    pub len: u32,
}

/// The longest run one record holds: 4 length bits + 3 extra bytes.
pub const MAX_RUN: u64 = (1 << 28) - 1;

/// The runs of one layer of `total` pixels. Zero-length runs are skipped.
pub fn decode(data: &[u8], total: u64) -> Result<Vec<Run>, String> {
    if data.first() != Some(&0x55) {
        return Err("layer data does not start with 0x55".into());
    }
    let end = data.len() - 1;
    let body = if end >= 1 { &data[1..end] } else { &[][..] };
    let sum = body.iter().fold(0u8, |s, &b| s.wrapping_add(b));
    if !sum != data[end] {
        return Err("layer checksum does not match".into());
    }
    let mut runs = Vec::with_capacity(body.len() / 2);
    let mut pixel: u64 = 0;
    let mut i = 0;
    let n = body.len();
    while i < n {
        let b = body[i];
        let kind = b >> 6;
        if kind == 0b10 {
            return Err("layer uses step runs, which DragonFruit's encoder never writes".into());
        }
        let extra = ((b >> 4) & 3) as usize;
        let need = (kind == 0b01) as usize + extra;
        if i + need >= n {
            return Err("layer data truncated".into());
        }
        let mut at = i + 1;
        let value = match kind {
            0b01 => {
                at += 1;
                body[i + 1]
            }
            0b11 => 255,
            _ => 0,
        };
        let mut high: u64 = 0;
        for k in 0..extra {
            high = high * 256 + body[at + k] as u64;
        }
        let len = high * 16 + (b & 0x0F) as u64;
        if pixel + len > total {
            return Err("layer decodes to too many pixels".into());
        }
        if len > 0 {
            runs.push(Run { value, len: len as u32 });
        }
        pixel += len;
        i = at + extra;
    }
    if pixel != total {
        return Err(format!("layer decodes to {pixel} pixels, expected {total}"));
    }
    Ok(runs)
}

/// Builds layer data from a stream of (value, length) pieces, merging equal
/// neighbours so every run is maximal, as `goo.encode_layer` does.
pub struct Encoder {
    out: Vec<u8>,
    value: u8,
    len: u64,
    max_run: u64,
}

impl Default for Encoder {
    fn default() -> Self {
        Self::with_max_run(MAX_RUN)
    }
}

impl Encoder {
    pub fn with_max_run(max_run: u64) -> Self {
        Encoder { out: vec![0x55], value: 0, len: 0, max_run }
    }

    #[inline]
    pub fn push(&mut self, value: u8, len: u64) {
        if len == 0 {
            return;
        }
        if value == self.value || self.len == 0 {
            self.value = value;
            self.len += len;
        } else {
            self.flush();
            self.value = value;
            self.len = len;
        }
    }

    fn flush(&mut self) {
        let mut left = self.len;
        while left > 0 {
            let len = left.min(self.max_run);
            write_record(&mut self.out, self.value, len);
            left -= len;
        }
        self.len = 0;
    }

    pub fn finish(mut self) -> Vec<u8> {
        self.flush();
        let sum = self.out[1..].iter().fold(0u8, |s, &b| s.wrapping_add(b));
        self.out.push(!sum);
        self.out
    }
}

/// One run record with the fewest length bytes that hold it; the grey value
/// byte comes before the length bytes.
fn write_record(out: &mut Vec<u8>, value: u8, len: u64) {
    let kind: u8 = match value {
        0 => 0,
        255 => 3,
        _ => 1,
    };
    let extra = (len >= 16) as u8 + (len >= 1 << 12) as u8 + (len >= 1 << 20) as u8;
    out.push(kind << 6 | extra << 4 | (len & 0x0F) as u8);
    if kind == 1 {
        out.push(value);
    }
    let high = len >> 4;
    for k in (0..extra).rev() {
        out.push((high >> (8 * k as u32)) as u8);
    }
}

/// Encode row-major pixels (tests and round trips).
pub fn encode_pixels(pixels: &[u8]) -> Vec<u8> {
    encode_pixels_with(pixels, MAX_RUN)
}

pub fn encode_pixels_with(pixels: &[u8], max_run: u64) -> Vec<u8> {
    let mut enc = Encoder::with_max_run(max_run);
    let mut i = 0;
    while i < pixels.len() {
        let v = pixels[i];
        let mut j = i + 1;
        while j < pixels.len() && pixels[j] == v {
            j += 1;
        }
        enc.push(v, (j - i) as u64);
        i = j;
    }
    enc.finish()
}

/// Re-encode runs unchanged.
pub fn encode_runs(runs: &[Run]) -> Vec<u8> {
    let mut enc = Encoder::default();
    for r in runs {
        enc.push(r.value, r.len as u64);
    }
    enc.finish()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn expand(runs: &[Run]) -> Vec<u8> {
        runs.iter().flat_map(|r| std::iter::repeat(r.value).take(r.len as usize)).collect()
    }

    /// The Python tests' reference `rle()`: one record per maximal run.
    fn reference(pixels: &[u8]) -> Vec<u8> {
        let mut body = Vec::new();
        let mut i = 0;
        while i < pixels.len() {
            let mut j = i;
            while j < pixels.len() && pixels[j] == pixels[i] {
                j += 1;
            }
            let (len, value) = ((j - i) as u64, pixels[i]);
            let kind: u8 = if value == 0 { 0 } else if value == 255 { 3 } else { 1 };
            let mut extra: Vec<u8> = (0..4).rev().map(|k| ((len >> 4) >> (8 * k)) as u8).collect();
            while extra.first() == Some(&0) {
                extra.remove(0);
            }
            body.push(kind << 6 | (extra.len() as u8) << 4 | (len & 0xF) as u8);
            if kind == 1 {
                body.push(value);
            }
            body.extend(extra);
            i = j;
        }
        let sum = body.iter().fold(0u8, |s, &b| s.wrapping_add(b));
        let mut out = vec![0x55];
        out.extend(body);
        out.push(!sum);
        out
    }

    #[test]
    fn encode_pins_literal_bytes() {
        assert_eq!(
            encode_pixels(&[0, 255, 255, 0, 0, 128, 128, 0]),
            vec![0x55, 0x01, 0xC2, 0x02, 0x42, 0x80, 0x01, 0x77]
        );
    }

    #[test]
    fn length_byte_boundaries_match_the_reference() {
        let cases: Vec<Vec<u8>> = vec![
            vec![0],
            vec![255; 15],
            vec![255; 16],
            [vec![0; 4095], vec![7; 4096]].concat(),
            [vec![0; (1 << 20) - 1], vec![255; 1 << 20]].concat(),
            [0u8, 255, 1, 254, 128].repeat(50),
        ];
        for pixels in cases {
            let data = encode_pixels(&pixels);
            assert_eq!(data, reference(&pixels), "{} pixels", pixels.len());
            assert_eq!(expand(&decode(&data, pixels.len() as u64).unwrap()), pixels);
        }
    }

    #[test]
    fn exact_boundaries_pick_the_fewest_length_bytes() {
        for (len, extra) in [(15u64, 0u8), (16, 1), (4095, 1), (4096, 2), ((1 << 20) - 1, 2), (1 << 20, 3), (MAX_RUN, 3)] {
            let mut out = Vec::new();
            write_record(&mut out, 255, len);
            assert_eq!((out[0] >> 4) & 3, extra, "len {len}");
            assert_eq!(out.len(), 1 + extra as usize);
        }
    }

    #[test]
    fn splits_runs_longer_than_one_record() {
        let pixels = [vec![255u8; 250], vec![0; 3]].concat();
        let data = encode_pixels_with(&pixels, 100);
        assert_eq!(expand(&decode(&data, 253).unwrap()), pixels);
        // two full 100-pixel white records (6 * 16 + 4), then 50, then 3 black
        assert_eq!(data, vec![0x55, 0xD4, 0x06, 0xD4, 0x06, 0xD2, 0x03, 0x03, data[8]]);
        // and a real over-long run splits into MAX_RUN + the remainder
        let mut enc = Encoder::default();
        enc.push(0, MAX_RUN + 5);
        let data = enc.finish();
        assert_eq!(&data[1..6], &[0x3F, 0xFF, 0xFF, 0xFF, 0x05]);
        let runs = decode(&data, MAX_RUN + 5).unwrap();
        assert_eq!(runs, vec![Run { value: 0, len: MAX_RUN as u32 }, Run { value: 0, len: 5 }]);
    }

    #[test]
    fn decoder_refuses_what_the_python_refuses() {
        let good = encode_pixels(&[0, 255, 255, 0, 0, 128, 128, 0]);
        assert!(decode(&good, 8).is_ok());
        assert!(decode(&[], 8).unwrap_err().contains("0x55"));
        assert!(decode(&[0x54, 0xFF], 8).unwrap_err().contains("0x55"));
        assert!(decode(&[0x55], 8).unwrap_err().contains("checksum"));
        let mut bad = good.clone();
        *bad.last_mut().unwrap() ^= 1;
        assert!(decode(&bad, 8).unwrap_err().contains("checksum"));
        assert!(decode(&good, 7).unwrap_err().contains("too many"));
        assert!(decode(&good, 9).unwrap_err().contains("expected 9"));
        // a step run (TT 10)
        assert!(decode(&[0x55, 0x81, !0x81], 1).unwrap_err().contains("step"));
        // a grey run missing its value byte; a run missing a length byte
        assert!(decode(&[0x55, 0x41, !0x41], 1).unwrap_err().contains("truncated"));
        assert!(decode(&[0x55, 0xD0, !0xD0], 16).unwrap_err().contains("truncated"));
        assert!(decode(&[0x55, 0x51, 0x80, !0xD1u8], 17).unwrap_err().contains("truncated"));
    }

    #[test]
    fn random_layers_round_trip() {
        let mut seed: u64 = 1;
        let mut rand = move || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        let mut pixels = Vec::new();
        for _ in 0..400 {
            let v = [0u8, 255, 64, 200][(rand() % 4) as usize];
            let n = 1 + (rand() % 3000) as usize;
            pixels.extend(std::iter::repeat(v).take(n));
        }
        let data = encode_pixels(&pixels);
        assert_eq!(data, reference(&pixels));
        let runs = decode(&data, pixels.len() as u64).unwrap();
        assert_eq!(encode_runs(&runs), data);
    }
}
