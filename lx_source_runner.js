#!/usr/bin/env node
/**
 * 落雪音乐音源脚本运行器（Node 版）
 * 兼容 lx-music 自定义音源脚本协议：EVENT_NAMES / on / send / request / httpFetch / utils / env / version / currentScriptInfo
 * 用法：
 *   node lx_source_runner.js <script.js> --info          # 初始化脚本，打印 sources
 *   node lx_source_runner.js <script.js>                 # 从 stdin 读 JSON 行请求，输出 JSON 行结果
 * 请求格式：{"id":1,"source":"kw","type":"320k","musicInfo":{"songmid":"...","hash":"...","copyrightId":"..."}}
 * 输出格式：{"id":1,"ok":true,"url":"..."} / {"id":1,"ok":false,"error":"..."}
 */
'use strict';
const fs = require('fs');
const vm = require('vm');
const crypto = require('crypto');
const zlib = require('zlib');

const scriptPath = process.argv[2];
const infoMode = process.argv.includes('--info');
if (!scriptPath || !fs.existsSync(scriptPath)) {
  console.error(JSON.stringify({ ok: false, error: 'script-not-found' }));
  process.exit(1);
}

const EVENT_NAMES = {
  inited: 'inited',
  request: 'request',
  updateAlert: 'updateAlert',
};

let initedPayload = null;
const handlers = {};
let scriptInfo = { name: '', description: '', version: '', author: '', homepage: '' };

// 从脚本头注释解析 @name @description @version @author @homepage
try {
  const raw = fs.readFileSync(scriptPath, 'utf8');
  const head = raw.slice(0, 2000);
  const meta = (re) => { const m = head.match(re); return m ? m[1].trim() : ''; };
  scriptInfo = {
    name: meta(/@name\s+(.+)/),
    description: meta(/@description\s+(.+)/),
    version: meta(/@version\s+(.+)/),
    author: meta(/@author\s+(.+)/),
    homepage: meta(/@homepage\s+(.+)/),
  };
} catch (e) { /* ignore */ }

function httpRequest(url, options = {}, callback = null) {
  return new Promise((resolve) => {
    const finish = (err, resp) => {
      if (typeof callback === 'function') { try { callback(err, resp); } catch (e) {} }
      resolve({ err, resp });
    };
    let parsed;
    try { parsed = new URL(url); } catch (e) { return finish(e, null); }
    if (!['http:', 'https:'].includes(parsed.protocol)) return finish(new Error('bad-protocol'), null);
    const mod = parsed.protocol === 'https:' ? require('https') : require('http');
    const method = String(options.method || 'GET').toUpperCase();
    let body = options.body ?? options.form ?? null;
    if (body && typeof body !== 'string' && typeof body !== 'Buffer') {
      body = typeof body === 'object' ? JSON.stringify(body) : String(body);
    }
    const headers = Object.assign({}, options.headers || {});
    const req = mod.request(parsed, {
      method,
      headers,
      timeout: Number(options.timeout) > 0 ? Number(options.timeout) : 20000,
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        let buf = Buffer.concat(chunks);
        const encoding = (res.headers['content-encoding'] || '').toLowerCase();
        try {
          if (encoding.includes('gzip')) buf = zlib.gunzipSync(buf);
          else if (encoding.includes('deflate')) buf = zlib.inflateSync(buf);
          else if (encoding.includes('br')) buf = zlib.brotliDecompressSync(buf);
        } catch (e) { /* 按原始数据处理 */ }
        let text = buf.toString('utf8');
        let data = text;
        const contentType = String(res.headers['content-type'] || '');
        if (contentType.includes('json') || /[[{"]/.test(text.slice(0, 1))) {
          try { data = JSON.parse(text); } catch (e) { /* 保留文本 */ }
        }
        finish(null, {
          statusCode: res.statusCode,
          status: res.statusCode,
          headers: res.headers,
          body: data,
          raw: buf,
        });
      });
    });
    req.on('timeout', () => { req.destroy(new Error('timeout')); });
    req.on('error', (e) => finish(e, null));
    if (body && method !== 'GET') req.write(body);
    req.end();
  });
}

function b64ToBuf(s) { return Buffer.from(String(s), 'base64'); }

const utils = {
  buffer: {
    from: (data, encoding) => {
      if (encoding) return Buffer.from(String(data), encoding);
      if (data instanceof Uint8Array) return Buffer.from(data);
      return Buffer.from(String(data), 'utf8');
    },
    toString: (buf, encoding = 'utf8') => Buffer.from(buf).toString(encoding),
  },
  crypto: {
    md5: (data) => crypto.createHash('md5').update(data).digest('hex'),
    randomBytes: (n) => crypto.randomBytes(n).toString('hex'),
    aesEn: (data, mode, key, iv) => {
      const dataBuf = Buffer.isBuffer(data) ? data : Buffer.from(String(data), 'utf8');
      const algo = String(mode).toLowerCase().includes('192') ? 'aes-192-cbc'
        : String(mode).toLowerCase().includes('256') ? 'aes-256-cbc' : 'aes-128-cbc';
      const cipher = crypto.createCipheriv(algo, Buffer.from(String(key)), iv ? Buffer.from(String(iv)) : null);
      return Buffer.concat([cipher.update(dataBuf), cipher.final()]).toString('base64');
    },
    aesDe: (data, mode, key, iv) => {
      const dataBuf = b64ToBuf(data);
      const algo = String(mode).toLowerCase().includes('192') ? 'aes-192-cbc'
        : String(mode).toLowerCase().includes('256') ? 'aes-256-cbc' : 'aes-128-cbc';
      const decipher = crypto.createDecipheriv(algo, Buffer.from(String(key)), iv ? Buffer.from(String(iv)) : null);
      return Buffer.concat([decipher.update(dataBuf), decipher.final()]).toString('utf8');
    },
    rsaEncrypt: () => { throw new Error('rsa-unsupported-in-runner'); },
  },
  zlib: {
    inflate: (buf) => zlib.inflateSync(Buffer.from(buf)).toString('utf8'),
    deflate: (data) => zlib.deflateSync(Buffer.from(String(data), 'utf8')),
    gzip: (data) => zlib.gzipSync(Buffer.from(String(data), 'utf8')),
  },
};

const sandboxLx = {
  EVENT_NAMES,
  env: 'web',
  version: '2.0.0',
  currentScriptInfo: Object.assign({ rawScript: scriptPath }, scriptInfo),
  utils,
  on: (name, handler) => { handlers[name] = handler; },
  send: (name, payload) => {
    if (name === EVENT_NAMES.inited) initedPayload = payload;
  },
  request: httpRequest,
  httpFetch: httpRequest,
};

// 脚本执行环境
const noop = () => {};
const sandboxConsole = { log: noop, warn: noop, error: noop, info: noop, debug: noop, trace: noop,
  group: noop, groupEnd: noop, groupCollapsed: noop, table: noop, dir: noop, time: noop, timeEnd: noop, assert: noop, count: noop };
const sandbox = {
  globalThis: undefined,
  lx: sandboxLx,
  console: sandboxConsole,
  setTimeout, clearTimeout, setInterval: () => {}, clearInterval: () => {},
  Buffer, URL, TextEncoder, TextDecoder,
  JSON, Math, Date, Array, Object, String, Number, Boolean, Promise, Error, RegExp, Map, Set,
  parseInt, parseFloat, isNaN, isFinite, encodeURIComponent, decodeURIComponent,
  atob: (s) => Buffer.from(s, 'base64').toString('binary'),
  btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
sandbox.window = sandbox;

try {
  const code = fs.readFileSync(scriptPath, 'utf8');
  vm.runInNewContext(code, sandbox, { filename: scriptPath, timeout: 15000 });
} catch (e) {
  console.error(JSON.stringify({ ok: false, error: 'script-load-failed: ' + e.message }));
  process.exit(1);
}

function pickHandler() {
  return handlers[EVENT_NAMES.request] || null;
}

async function callMusicUrl(payload) {
  const handler = pickHandler();
  if (!handler) throw new Error('script-has-no-request-handler');
  const info = { musicInfo: payload.musicInfo || {}, type: payload.type || '128k' };
  const result = await Promise.race([
    Promise.resolve(handler({ source: payload.source, action: payload.action || 'musicUrl', info })),
    new Promise((_, rej) => setTimeout(() => rej(new Error('script-timeout')), 30000)),
  ]);
  // 兼容返回字符串或 {url}
  if (typeof result === 'string') return result;
  if (result && typeof result === 'object') {
    if (result.url) return result;
    if (result.code && result.msg) throw new Error(String(result.msg));
  }
  throw new Error('script-empty-result');
}

if (infoMode) {
  // 等待脚本异步初始化（部分脚本 init 时要拉远端数据）
  const waitInit = async () => {
    for (let i = 0; i < 60; i++) {
      if (initedPayload) break;
      await new Promise((r) => setTimeout(r, 500));
    }
    const sources = (initedPayload && initedPayload.sources) || {};
    console.log(JSON.stringify({ ok: true, sources, name: scriptInfo.name, info: scriptInfo }));
    process.exit(0);
  };
  waitInit();
} else {
  const readline = require('readline');
  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', async (line) => {
    let req;
    try { req = JSON.parse(line); } catch (e) { return; }
    try {
      const result = await callMusicUrl(req);
      if (typeof result === 'string') {
        process.stdout.write(JSON.stringify({ id: req.id, ok: true, url: result }) + '\n');
      } else {
        const out = { id: req.id, ok: true, url: (result && result.url) || '' };
        for (const k of ['type', 'size', 'quality', 'bitrate', 'format']) {
          if (result && result[k] !== undefined) out[k] = result[k];
        }
        process.stdout.write(JSON.stringify(out) + '\n');
      }
    } catch (e) {
      process.stdout.write(JSON.stringify({ id: req.id, ok: false, error: String(e && e.message || e) }) + '\n');
    }
    // 单次请求处理完即退出，避免脚本内部的定时器/长连接让进程挂住
    setTimeout(() => process.exit(0), 200);
  });
}
