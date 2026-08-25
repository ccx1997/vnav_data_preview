#!/usr/bin/env node
/**
 * 从看板导出的 JSONL / ZIP / video_segments.json 下载 OSS 录像段。
 * 训练机推荐 Python：tools/pull-oss-videos.py（拉段 + 按子任务 keep_windows 拼接）。
 *
 * 用法:
 *   node tools/download-video-segments.mjs --in meta_<task>_<i>.jsonl --out ./vids
 *   node tools/download-video-segments.mjs --in meta_<task>_<i>.zip --out ./vids
 *   node tools/download-video-segments.mjs --in video_segments.json --out ./vids --dry-run
 *
 * 契约字段: segments[].camera, start_ts, end_ts, clip_from_ts, clip_to_ts, url, object_key
 * URL 为 OSS presigned，有时效；过期请重新导出。拼接请用 pull-oss-videos.py。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import http from 'node:http';
import https from 'node:https';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function usage() {
  console.log(`Usage: node download-video-segments.mjs --in <jsonl|json|zip> --out <dir> [--dry-run] [--limit N]
  --in       meta_*.jsonl / meta_*.zip / video_segments.json
  --out      输出目录（按 camera/ 分子目录）
  --dry-run  只打印将请求的 URL，不写盘
  --limit N  最多下载 N 段（调试）
  拼接连续 MP4 请用: python3 pull-oss-videos.py --in <jsonl|zip> --out <dir>
`);
}

function parseArgs(argv) {
  const out = { in: null, out: null, dryRun: false, limit: Infinity };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--in') out.in = argv[++i];
    else if (a === '--out') out.out = argv[++i];
    else if (a === '--dry-run') out.dryRun = true;
    else if (a === '--limit') out.limit = parseInt(argv[++i], 10) || Infinity;
    else if (a === '-h' || a === '--help') {
      usage();
      process.exit(0);
    }
  }
  return out;
}

/** 读取 JSONL / JSON / ZIP 内 video_segments；返回 { segments, camera_positions, meta } */
export function loadVideoSegmentsPayload(inputPath) {
  const abs = path.resolve(inputPath);
  if (!fs.existsSync(abs)) throw new Error(`input not found: ${abs}`);
  const buf = fs.readFileSync(abs);
  if (buf[0] === 0x50 && buf[1] === 0x4b) {
    const text = extractZipEntry(buf, 'video_segments.json');
    if (!text) throw new Error('ZIP missing video_segments.json');
    return normalizePayload(JSON.parse(text));
  }
  const text = buf.toString('utf8');
  if (abs.endsWith('.jsonl') || text.trimStart().startsWith('{"_task"') || text.trimStart().startsWith('{"_kind"')) {
    const first = text.split('\n').find((line) => line.trim());
    if (!first) throw new Error('jsonl empty');
    const row = JSON.parse(first);
    return normalizePayload({
      task_id: row._task?.task_id,
      sub_task_id: row._task?.sub_task?.sub_task_id,
      camera_positions: row._camera_positions || {},
      segments: row._video_segments || [],
    });
  }
  return normalizePayload(JSON.parse(text));
}

function normalizePayload(raw) {
  if (Array.isArray(raw)) {
    return { segments: raw, camera_positions: {}, meta: {} };
  }
  const segments = Array.isArray(raw.segments)
    ? raw.segments
    : Array.isArray(raw.video_segments)
      ? raw.video_segments
      : Array.isArray(raw._video_segments)
        ? raw._video_segments
        : [];
  const task = raw._task && typeof raw._task === 'object' ? raw._task : {};
  return {
    segments,
    camera_positions: raw.camera_positions || raw._camera_positions || {},
    meta: {
      task_id: raw.task_id || task.task_id,
      sub_task_id: raw.sub_task_id || task.sub_task?.sub_task_id,
      video_quality: raw.video_quality,
      video_cameras: raw.video_cameras,
      count: raw.count,
    },
  };
}

/** 极简 ZIP store 解压：找 local file header 名匹配 entry */
function extractZipEntry(buf, name) {
  let offset = 0;
  while (offset + 30 < buf.length) {
    if (buf.readUInt32LE(offset) !== 0x04034b50) break;
    const method = buf.readUInt16LE(offset + 8);
    const compSize = buf.readUInt32LE(offset + 18);
    const nameLen = buf.readUInt16LE(offset + 26);
    const extraLen = buf.readUInt16LE(offset + 28);
    const entryName = buf.slice(offset + 30, offset + 30 + nameLen).toString('utf8');
    const dataStart = offset + 30 + nameLen + extraLen;
    const data = buf.slice(dataStart, dataStart + compSize);
    if (entryName === name || entryName.endsWith('/' + name)) {
      if (method !== 0) throw new Error(`ZIP entry ${name} is compressed (method=${method}); use store-only ZIP`);
      return data.toString('utf8');
    }
    offset = dataStart + compSize;
  }
  return null;
}

function segmentFileName(seg, index) {
  const cam = String(seg.camera || 'cam').replace(/[^\w.-]+/g, '_');
  const start = Number.isFinite(Number(seg.start_ts)) ? Math.floor(Number(seg.start_ts)) : index;
  return `${cam}_${start}.mp4`;
}

function fetchToFile(url, destPath, redirects = 0) {
  return new Promise((resolve, reject) => {
    const lib = url.startsWith('https:') ? https : http;
    const req = lib.get(url, { timeout: 30_000 }, (res) => {
      if (res.statusCode && res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        if (redirects > 5) {
          reject(new Error('too many redirects'));
          return;
        }
        fetchToFile(res.headers.location, destPath, redirects + 1).then(resolve, reject);
        return;
      }
      if (!res.statusCode || res.statusCode >= 400) {
        res.resume();
        reject(new Error(`HTTP ${res.statusCode} for ${url.slice(0, 80)}…`));
        return;
      }
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        try {
          const body = Buffer.concat(chunks);
          fs.writeFileSync(destPath, body);
          resolve({ bytes: body.length });
        } catch (e) {
          reject(e);
        }
      });
      res.on('error', reject);
    });
    req.on('timeout', () => {
      req.destroy(new Error('request timeout'));
    });
    req.on('error', reject);
  });
}

/** 供单测：解析 payload 并规划输出路径，不真正下载 */
export function planDownloads(payload, outDir) {
  const segments = payload.segments || [];
  return segments.map((seg, i) => {
    const cam = String(seg.camera || 'unknown');
    const pos = seg.physical_position
      || (payload.camera_positions && payload.camera_positions[cam])
      || null;
    const file = segmentFileName(seg, i);
    return {
      index: i,
      camera: cam,
      physical_position: pos,
      url: seg.url || null,
      object_key: seg.object_key || null,
      dest: path.join(outDir, cam, file),
    };
  });
}

/** 执行下载计划；返回统计。dryRun 时不写盘。 */
export async function runDownloads(plans, { dryRun = false } = {}) {
  let ok = 0;
  let skip = 0;
  let fail = 0;
  for (const p of plans) {
    const posZh = p.physical_position?.position_zh || p.physical_position?.position_en || '';
    if (!p.url) {
      console.warn(`[skip] ${p.camera} no url ${posZh ? '(' + posZh + ')' : ''}`);
      skip++;
      continue;
    }
    if (dryRun) {
      console.log(`[dry-run] ${p.dest} <- ${String(p.url).slice(0, 100)}… ${posZh}`);
      ok++;
      continue;
    }
    fs.mkdirSync(path.dirname(p.dest), { recursive: true });
    try {
      const r = await fetchToFile(p.url, p.dest);
      console.log(`[ok] ${p.dest} (${r.bytes}B) ${posZh}`);
      ok++;
    } catch (e) {
      console.error(`[fail] ${p.dest}: ${e.message}`);
      fail++;
    }
  }
  console.log(`done ok=${ok} skip=${skip} fail=${fail}`);
  return { ok, skip, fail };
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.in || !args.out) {
    usage();
    process.exit(2);
  }
  const payload = loadVideoSegmentsPayload(args.in);
  const plans = planDownloads(payload, path.resolve(args.out)).slice(0, args.limit);
  console.log(`segments=${payload.segments.length} planned=${plans.length} task=${payload.meta.task_id || '—'}`);
  if (payload.camera_positions && Object.keys(payload.camera_positions).length) {
    console.log('camera_positions:', JSON.stringify(payload.camera_positions));
  }
  const stats = await runDownloads(plans, { dryRun: args.dryRun });
  if (stats.fail > 0) process.exit(1);
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  main().catch((e) => {
    console.error(e);
    process.exit(1);
  });
}
