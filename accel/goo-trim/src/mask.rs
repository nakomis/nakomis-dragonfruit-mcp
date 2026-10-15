//! Bit-packed masks over a crop of the screen, and the two operations the
//! trim needs: dilation with a 3x3 cross and 8-connected flood fill.
//!
//! A crop is whole 64-pixel words wide (bit i of word k is pixel 64k + i of
//! the row), so masks of different layers line up word for word. Pixels
//! outside a crop are 0, as they are outside the screen.

use crate::rle::Run;

pub const MARGIN_PX: usize = 8;

#[derive(Clone, Debug)]
pub struct Mask {
    /// First word column (pixel x = 64 * wx0) and first row.
    pub wx0: usize,
    pub y0: usize,
    /// Words per row, rows.
    pub ww: usize,
    pub h: usize,
    pub bits: Vec<u64>,
}

impl Mask {
    pub fn empty() -> Self {
        Mask { wx0: 0, y0: 0, ww: 0, h: 0, bits: Vec::new() }
    }

    pub fn zeros_like(other: &Mask) -> Self {
        Mask { bits: vec![0; other.bits.len()], ..*other }
    }

    fn zeros(wx0: usize, y0: usize, ww: usize, h: usize) -> Self {
        Mask { wx0, y0, ww, h, bits: vec![0; ww * h] }
    }

    #[inline]
    pub fn get(&self, x: usize, y: usize) -> bool {
        self.bits[y * self.ww + (x >> 6)] >> (x & 63) & 1 != 0
    }

    #[inline]
    pub fn set(&mut self, x: usize, y: usize) {
        self.bits[y * self.ww + (x >> 6)] |= 1 << (x & 63);
    }

    #[inline]
    pub fn clear(&mut self, x: usize, y: usize) {
        self.bits[y * self.ww + (x >> 6)] &= !(1u64 << (x & 63));
    }

    /// Crop-relative row `y`, pixels [xa, xb).
    fn set_range(&mut self, y: usize, xa: usize, xb: usize) {
        let row = &mut self.bits[y * self.ww..(y + 1) * self.ww];
        let (ka, kb) = (xa >> 6, (xb - 1) >> 6);
        let first = !0u64 << (xa & 63);
        let last = !0u64 >> (63 - ((xb - 1) & 63));
        if ka == kb {
            row[ka] |= first & last;
        } else {
            row[ka] |= first;
            for w in &mut row[ka + 1..kb] {
                *w = !0;
            }
            row[kb] |= last;
        }
    }

    pub fn count(&self) -> u64 {
        self.bits.iter().map(|w| w.count_ones() as u64).sum()
    }

    pub fn is_empty(&self) -> bool {
        self.bits.iter().all(|&w| w == 0)
    }

    /// `self & !other` (same crop).
    pub fn and_not(&self, other: &Mask) -> Mask {
        let bits = self.bits.iter().zip(&other.bits).map(|(a, b)| a & !b).collect();
        Mask { bits, ..*self }
    }

    /// `other`'s bits, seen through this mask's crop.
    pub fn project(&self, other: &Mask) -> Mask {
        let mut out = Mask::zeros_like(self);
        let (ya, yb) = (self.y0.max(other.y0), (self.y0 + self.h).min(other.y0 + other.h));
        let (ka, kb) = (self.wx0.max(other.wx0), (self.wx0 + self.ww).min(other.wx0 + other.ww));
        if ya < yb && ka < kb {
            for y in ya..yb {
                let src = (y - other.y0) * other.ww + (ka - other.wx0);
                let dst = (y - self.y0) * self.ww + (ka - self.wx0);
                out.bits[dst..dst + kb - ka].copy_from_slice(&other.bits[src..src + kb - ka]);
            }
        }
        out
    }

    /// Dilate `times` times with a 3x3 cross: every pixel within L1 distance
    /// `times` of a set pixel (OpenCV's `dilate` with MORPH_CROSS, iterated).
    pub fn dilate_cross(&self, times: usize) -> Mask {
        let mut cur = self.clone();
        let mut next = Mask::zeros_like(self);
        let (ww, h) = (self.ww, self.h);
        for _ in 0..times {
            for y in 0..h {
                let row = &cur.bits[y * ww..(y + 1) * ww];
                let up = if y > 0 { Some(&cur.bits[(y - 1) * ww..y * ww]) } else { None };
                let dn = if y + 1 < h { Some(&cur.bits[(y + 1) * ww..(y + 2) * ww]) } else { None };
                let out = &mut next.bits[y * ww..(y + 1) * ww];
                for k in 0..ww {
                    let c = row[k];
                    let left = (c << 1) | if k > 0 { row[k - 1] >> 63 } else { 0 };
                    let right = (c >> 1) | if k + 1 < ww { row[k + 1] << 63 } else { 0 };
                    let mut v = c | left | right;
                    if let Some(u) = up {
                        v |= u[k];
                    }
                    if let Some(d) = dn {
                        v |= d[k];
                    }
                    out[k] = v;
                }
            }
            std::mem::swap(&mut cur, &mut next);
        }
        cur
    }

    /// Crop-relative row `y`: the first x in [x, end) whose bit is not `bit`, or `end`.
    #[inline]
    pub fn next_change(&self, y: usize, mut x: usize, end: usize, bit: bool) -> usize {
        let row = &self.bits[y * self.ww..(y + 1) * self.ww];
        while x < end {
            let k = x >> 6;
            let word = if bit { !row[k] } else { row[k] } >> (x & 63);
            if word != 0 {
                return (x + word.trailing_zeros() as usize).min(end);
            }
            x = (k + 1) << 6;
        }
        end
    }
}

/// Where lit pixels are: `None` for a black layer.
pub struct Masks {
    pub lit: Mask,
    pub core: Mask,
}

/// Bounding box (x0, y0, x1, y1 inclusive) of the pixels whose value passes `keep`.
fn bbox(runs: &[Run], width: usize, keep: impl Fn(u8) -> bool) -> Option<(usize, usize, usize, usize)> {
    let (mut x0, mut y0, mut x1, mut y1) = (usize::MAX, usize::MAX, 0, 0);
    let mut p = 0usize;
    let mut any = false;
    for r in runs {
        let q = p + r.len as usize;
        if keep(r.value) {
            any = true;
            let (ra, rb) = (p / width, (q - 1) / width);
            y0 = y0.min(ra);
            y1 = y1.max(rb);
            if ra == rb {
                x0 = x0.min(p % width);
                x1 = x1.max((q - 1) % width);
            } else {
                x0 = 0;
                x1 = width - 1;
            }
        }
        p = q;
    }
    any.then_some((x0, y0, x1, y1))
}

/// Lit (grey > 0) and core (grey >= `core_at`) masks over the bounding box of
/// the pixels passing `crop_on`, plus MARGIN_PX, rounded out to whole words.
pub fn build(
    runs: &[Run],
    width: usize,
    height: usize,
    core_at: u8,
    crop_on: impl Fn(u8) -> bool,
    want_lit: bool,
) -> Option<Masks> {
    let (x0, y0, x1, y1) = bbox(runs, width, crop_on)?;
    let xa = x0.saturating_sub(MARGIN_PX);
    let xb = (x1 + MARGIN_PX + 1).min(width);
    let ya = y0.saturating_sub(MARGIN_PX);
    let yb = (y1 + MARGIN_PX + 1).min(height);
    let wx0 = xa >> 6;
    let ww = ((xb + 63) >> 6) - wx0;
    let mut lit = if want_lit { Mask::zeros(wx0, ya, ww, yb - ya) } else { Mask::empty() };
    let mut core = Mask::zeros(wx0, ya, ww, yb - ya);
    let base = wx0 << 6;
    let mut p = 0usize;
    for r in runs {
        let q = p + r.len as usize;
        let is_core = r.value >= core_at;
        if (want_lit && r.value > 0) || is_core {
            for y in p / width..=(q - 1) / width {
                if y < ya || y >= yb {
                    continue; // only possible for pixels outside the crop's own criterion
                }
                let a = p.max(y * width) - y * width;
                let b = q.min((y + 1) * width) - y * width;
                let (a, b) = (a.max(xa), b.min(xb));
                if a >= b {
                    continue;
                }
                if want_lit && r.value > 0 {
                    lit.set_range(y - ya, a - base, b - base);
                }
                if is_core {
                    core.set_range(y - ya, a - base, b - base);
                }
            }
        }
        p = q;
    }
    Some(Masks { lit, core })
}

/// 8-connected components of `core` that touch none of `below`, found by
/// flooding from each core pixel outside `below` and stopping as soon as a
/// flood reaches `below` (or a component already known to be held).
/// Returns the unheld pixels and each unheld component's size, in raster
/// order of the component's first pixel.
pub struct Flood {
    pub unheld: Mask,
    pub sizes: Vec<u64>,
}

pub fn unheld_components(core: &Mask, below: &Mask) -> Flood {
    let suspect = core.and_not(below);
    let mut unheld = Mask::zeros_like(core);
    let mut sizes = Vec::new();
    if suspect.is_empty() {
        return Flood { unheld, sizes };
    }
    let (ww, h) = (core.ww, core.h);
    let wpx = ww * 64;
    let mut visited = Mask::zeros_like(core);
    let mut current = Mask::zeros_like(core);
    let mut stack: Vec<(u32, u32)> = Vec::new();
    let mut members: Vec<(u32, u32)> = Vec::new();
    for y in 0..h {
        for k in 0..ww {
            let mut word = suspect.bits[y * ww + k];
            while word != 0 {
                let x = (k << 6) + word.trailing_zeros() as usize;
                word &= word - 1;
                if visited.get(x, y) {
                    continue;
                }
                stack.clear();
                members.clear();
                visited.set(x, y);
                current.set(x, y);
                stack.push((x as u32, y as u32));
                members.push((x as u32, y as u32));
                let mut held = false;
                'flood: while let Some((px, py)) = stack.pop() {
                    let (px, py) = (px as usize, py as usize);
                    for ny in py.saturating_sub(1)..=(py + 1).min(h - 1) {
                        for nx in px.saturating_sub(1)..=(px + 1).min(wpx - 1) {
                            if !core.get(nx, ny) || current.get(nx, ny) {
                                continue;
                            }
                            if visited.get(nx, ny) || below.get(nx, ny) {
                                // A held component (an earlier flood stopped in it) or below itself.
                                visited.set(nx, ny);
                                held = true;
                                break 'flood;
                            }
                            visited.set(nx, ny);
                            current.set(nx, ny);
                            stack.push((nx as u32, ny as u32));
                            members.push((nx as u32, ny as u32));
                        }
                    }
                }
                for &(mx, my) in &members {
                    current.clear(mx as usize, my as usize);
                    if !held {
                        unheld.set(mx as usize, my as usize);
                    }
                }
                if !held {
                    sizes.push(members.len() as u64);
                }
            }
        }
    }
    Flood { unheld, sizes }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn from_rows(rows: &[&str]) -> Mask {
        let mut m = Mask::zeros(0, 0, 1, rows.len());
        for (y, r) in rows.iter().enumerate() {
            for (x, c) in r.chars().enumerate() {
                if c == '#' {
                    m.set(x, y);
                }
            }
        }
        m
    }

    #[test]
    fn dilation_is_the_l1_ball() {
        let mut m = Mask::zeros(0, 0, 2, 20);
        m.set(63, 10);
        let d = m.dilate_cross(3);
        for y in 0..20 {
            for x in 0..128 {
                let l1 = (x as i64 - 63).abs() + (y as i64 - 10).abs();
                assert_eq!(d.get(x, y), l1 <= 3, "{x},{y}");
            }
        }
    }

    #[test]
    fn flood_is_8_connected_and_respects_below() {
        let core = from_rows(&["#...##", ".#..##", "......", "...#.."]);
        let below = from_rows(&["....#.", "......", "......", "......"]);
        let f = unheld_components(&core, &below);
        // the diagonal pair is one component, the 2x2 block touches below, the dot is alone
        assert_eq!(f.sizes, vec![2, 1]);
        assert!(f.unheld.get(0, 0) && f.unheld.get(1, 1) && f.unheld.get(3, 3));
        assert!(!f.unheld.get(4, 0) && !f.unheld.get(5, 1));
    }

    #[test]
    fn build_masks_crop_and_threshold() {
        // 70 wide, 3 rows: grey 100 on row 1 at 65..68, white across the 0/1 row boundary
        let runs = vec![
            Run { value: 0, len: 68 },
            Run { value: 255, len: 4 }, // row 0: 68, 69; row 1: 0, 1
            Run { value: 0, len: 63 },
            Run { value: 100, len: 3 }, // row 1: 65..68
            Run { value: 0, len: 72 },
        ];
        let m = build(&runs, 70, 3, 128, |v| v > 0, true).unwrap();
        assert_eq!((m.lit.wx0, m.lit.ww, m.lit.y0, m.lit.h), (0, 2, 0, 3));
        assert!(m.core.get(69, 0) && m.core.get(0, 1) && !m.core.get(66, 1));
        assert!(m.lit.get(66, 1) && !m.lit.get(64, 1));
        assert_eq!((m.lit.count(), m.core.count()), (7, 4));
        assert!(build(&[Run { value: 0, len: 210 }], 70, 3, 128, |v| v > 0, true).is_none());
    }

    #[test]
    fn next_change_scans_words() {
        let mut m = Mask::zeros(0, 0, 3, 1);
        m.set_range(0, 10, 150);
        assert_eq!(m.next_change(0, 0, 192, false), 10);
        assert_eq!(m.next_change(0, 10, 192, true), 150);
        assert_eq!(m.next_change(0, 150, 192, false), 192);
        assert_eq!(m.next_change(0, 20, 100, true), 100);
    }
}
