// 產生 PWA 圖示 PNG，不依賴任何第三方套件（只用 Node 內建 zlib）。
// 設計：品牌深藍圓角底 + 白色「AU」點陣字 + 琥珀色底線，
// 配色取自站上的 --navbg (#10233b) 與 --star (#e0b84a)。
//
// 作法：先填背景，再用「整數矩形填色」畫字，避免逐點取樣造成的 off-by-one 鋸齒。
const zlib = require("zlib");
const fs = require("fs");

const NAVY = [0x10, 0x23, 0x3b, 255];
const WHITE = [0xff, 0xff, 0xff, 255];
const AMBER = [0xe0, 0xb8, 0x4a, 255];
const CLEAR = [0, 0, 0, 0];

// 5x7 點陣字
const GLYPH = {
  A: [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
  U: ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
};

function crc32(buf) {
  let c, crc = 0xffffffff;
  for (let n = 0; n < buf.length; n++) {
    c = (crc ^ buf[n]) & 0xff;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    crc = (crc >>> 8) ^ c;
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const td = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(td), 0);
  return Buffer.concat([len, td, crc]);
}

function makeIcon(size, maskable) {
  // RGBA 畫布
  const buf = new Uint8Array(size * size * 4);
  const set = (x, y, c) => {
    if (x < 0 || y < 0 || x >= size || y >= size) return;
    const i = (y * size + x) * 4;
    buf[i] = c[0]; buf[i + 1] = c[1]; buf[i + 2] = c[2]; buf[i + 3] = c[3];
  };
  const fillRect = (x0, y0, w, h, c) => {
    for (let y = y0; y < y0 + h; y++) for (let x = x0; x < x0 + w; x++) set(x, y, c);
  };

  // 1) 底：maskable 整面填滿（讓系統自行裁切）；一般版畫圓角矩形
  if (maskable) {
    fillRect(0, 0, size, size, NAVY);
  } else {
    fillRect(0, 0, size, size, CLEAR);
    const inset = Math.round(size * 0.055);
    const r = Math.round(size * 0.225);
    const x0 = inset, y0 = inset, x1 = size - inset - 1, y1 = size - inset - 1;
    for (let y = y0; y <= y1; y++) {
      for (let x = x0; x <= x1; x++) {
        const dx = x < x0 + r ? x0 + r - x : x > x1 - r ? x - (x1 - r) : 0;
        const dy = y < y0 + r ? y0 + r - y : y > y1 - r ? y - (y1 - r) : 0;
        if (dx * dx + dy * dy <= r * r) set(x, y, NAVY);
      }
    }
  }

  // 2) 字：11 格寬（5 + 1 間距 + 5）、7 格高，用整數格寬避免累積誤差
  const cell = Math.round((maskable ? 0.5 : 0.6) * size / 11);
  const textW = cell * 11, textH = cell * 7;
  const tx = Math.round((size - textW) / 2);
  const ty = Math.round((size - textH) / 2 - size * 0.035);

  for (const [gi, ch] of ["A", "U"].entries()) {
    const colOff = gi * 6;                       // A 從第 0 格、U 從第 6 格開始
    for (let row = 0; row < 7; row++) {
      for (let col = 0; col < 5; col++) {
        if (GLYPH[ch][row][col] !== "#") continue;
        fillRect(tx + (colOff + col) * cell, ty + row * cell, cell, cell, WHITE);
      }
    }
  }

  // 3) 底線
  const barY = ty + textH + Math.round(size * 0.05);
  fillRect(tx, barY, textW, Math.max(2, Math.round(size * 0.045)), AMBER);

  // 4) 編碼成 PNG
  const raw = Buffer.alloc(size * (size * 4 + 1));
  let p = 0;
  for (let y = 0; y < size; y++) {
    raw[p++] = 0;                                 // filter: none
    buf.subarray(y * size * 4, (y + 1) * size * 4).forEach((v) => (raw[p++] = v));
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8;   // bit depth
  ihdr[9] = 6;   // RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", zlib.deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

const out = process.argv[2];
for (const [name, size, maskable] of [
  ["icon-192.png", 192, false],
  ["icon-512.png", 512, false],
  ["icon-maskable-512.png", 512, true],
]) {
  fs.writeFileSync(out + "/" + name, makeIcon(size, maskable));
  console.log(name, fs.statSync(out + "/" + name).size, "bytes");
}
