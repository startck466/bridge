/**
 * bridge 订阅解密端点（Cloudflare Worker）
 *
 * 仓库里只有 AES-256-GCM 密文（sub/bridge.enc），密钥是 Worker 的 Secret 变量
 * SUB_KEY，路径令牌是 Secret 变量 SUB_PATH。仓库公开也解不开。
 *
 * 环境变量（都在 Worker Secret 里，代码中不含）：
 *   SUB_KEY   base64(32 字节 AES-256 密钥)
 *   SUB_PATH  64 位 hex 路径令牌，即订阅 URL 的最后一段
 *
 * 可选普通变量：
 *   CIPHER_BASE  密文所在仓库前缀，默认指向 raw.githubusercontent.com
 *
 * 路由：GET /<SUB_PATH>  ->  明文 mihomo/FlClash 配置
 *      其他任何路径      ->  404（不区分"路径错"与"不存在"）
 */

const MAGIC = 'BRIDGE-SUB-v1';
const MAX_AGE = 300; // 5 分钟内的密文视为新鲜

// 常量时间比较，避免用长度/首字节差异做侧信道
function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const enc = new TextEncoder();
  const ab = enc.encode(a);
  const bb = enc.encode(b);
  let diff = ab.length ^ bb.length;
  const n = Math.max(ab.length, bb.length);
  for (let i = 0; i < n; i++) {
    diff |= (ab[i] || 0) ^ (bb[i] || 0);
  }
  return diff === 0;
}

function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function fetchCipher(env) {
  const bases = [];
  if (env.CIPHER_BASE) bases.push(env.CIPHER_BASE.replace(/\/+$/, ''));
  const bust = Math.floor(Date.now() / (MAX_AGE * 1000));
  bases.push('https://raw.githubusercontent.com/startck466/bridge/main/sub/bridge.enc');
  bases.push('https://fastly.jsdelivr.net/gh/startck466/bridge@main/sub/bridge.enc');

  let lastErr = null;
  for (const base of bases) {
    const sep = base.includes('?') ? '&' : '?';
    const url = base + sep + 'cb=' + bust;
    try {
      const r = await fetch(url, {
        cf: { cacheTtl: 0, cacheEverything: false },
        headers: { 'cache-control': 'no-cache' },
      });
      if (!r.ok) {
        lastErr = `HTTP ${r.status} @ ${base}`;
        continue;
      }
      const text = await r.text();
      if (!text || text.indexOf(MAGIC) !== 0) {
        lastErr = `bad magic @ ${base}`;
        continue;
      }
      return text;
    } catch (e) {
      lastErr = `${e.message} @ ${base}`;
    }
  }
  throw new Error('cipher unavailable: ' + lastErr);
}

async function decrypt(sealed, keyB64) {
  const lines = sealed.trim().split('\n');
  if (lines[0] !== MAGIC) throw new Error('unexpected envelope');
  const blob = b64ToBytes(lines[1].trim());
  if (blob.length < 12 + 16) throw new Error('cipher too short');

  const nonce = blob.slice(0, 12);
  const ct = blob.slice(12);

  const raw = b64ToBytes(keyB64);
  if (raw.length !== 32) throw new Error('bad key length');

  const key = await crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['decrypt']);
  const plain = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: nonce, additionalData: new TextEncoder().encode(MAGIC) },
    key,
    ct
  );
  return new TextDecoder('utf-8').decode(plain);
}

function notFound() {
  return new Response('Not Found', {
    status: 404,
    headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/^\/+|\/+$/g, '');

    // 根路径给一句人话，避免被误当成"服务在线"探针
    if (path === '') return notFound();

    if (!env.SUB_PATH || !env.SUB_KEY) {
      return new Response('endpoint not configured', { status: 503 });
    }
    if (!safeEqual(path, env.SUB_PATH)) return notFound();

    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method Not Allowed', {
        status: 405,
        headers: { allow: 'GET, HEAD' },
      });
    }

    try {
      const sealed = await fetchCipher(env);
      const body = await decrypt(sealed, env.SUB_KEY);

      const headers = {
        'content-type': 'text/yaml; charset=utf-8',
        'cache-control': 'no-store, no-cache, must-revalidate, private',
        'profile-update-interval': '24',
        'content-disposition': "attachment; filename*=UTF-8''bridge.yaml",
        'x-content-type-options': 'nosniff',
        'referrer-policy': 'no-referrer',
      };

      return new Response(request.method === 'HEAD' ? null : body, { status: 200, headers });
    } catch (e) {
      return new Response('upstream error', {
        status: 502,
        headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' },
      });
    }
  },
};
