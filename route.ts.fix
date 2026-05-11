import { NextRequest, NextResponse } from 'next/server';
import { createHash, createDecipheriv } from 'crypto';

// =====================================================
// 环境变量
// =====================================================
const WECOM_TOKEN = process.env.WECOM_TOKEN || '';
const WECOM_ENCODING_AES_KEY = process.env.WECOM_ENCODING_AES_KEY || '';
const WECOM_CORP_ID = process.env.WECOM_CORP_ID || '';

// =====================================================
// 辅助函数：SHA1 签名
// =====================================================
function sha1Signature(token: string, timestamp: string, nonce: string, encrypt: string): string {
  const arr = [token, timestamp, nonce, encrypt].sort();
  const str = arr.join('');
  return createHash('sha1').update(str, 'utf8').digest('hex');
}

// =====================================================
// 辅助函数：验证签名
// =====================================================
function verifySignature(
  token: string,
  timestamp: string,
  nonce: string,
  encrypt: string,
  signature: string
): boolean {
  const computed = sha1Signature(token, timestamp, nonce, encrypt);
  return computed === signature;
}

// =====================================================
// 辅助函数：解码 Base64 AES Key
// 企业微信的 EncodingAESKey 是 43 字符，需要补 '=' 后解码得到 32 字节密钥
// =====================================================
function decodeBase64AesKey(encodingAesKey: string): Buffer {
  const base64 = encodingAesKey + '=';
  return Buffer.from(base64, 'base64');
}

// =====================================================
// 辅助函数：PKCS#7 unpadding
// =====================================================
function pkcs7Unpadding(buffer: Uint8Array): Uint8Array {
  let pad = buffer[buffer.length - 1];
  if (pad < 1 || pad > 32) {
    pad = 0;
  }
  return buffer.subarray(0, buffer.length - pad);
}

// =====================================================
// 辅助函数：AES-CBC 解密企业微信消息
// 返回: { msg: string, corpId: string }
//
// 企业微信加密格式：
//   AES-Key  = Base64Decode(EncodingAESKey + "=")   → 32 字节
//   IV       = AES-Key 的前 16 字节（不是整个 key！）
//   解密结果  = 16字节random + 4字节(大端)msg_len + msg + corpId
// =====================================================
function decryptWeComMessage(encrypt: string, encodingAesKey: string): { msg: string; corpId: string } {
  const aesKey = decodeBase64AesKey(encodingAesKey);

  // ✅ FIX: IV must be the FIRST 16 BYTES of the AES key, NOT the full 32-byte key.
  //    The previous code used `const iv = aesKey` (32 bytes), which caused Node.js to
  //    throw "Invalid initialization vector" on every call, making all callbacks fail.
  const iv = aesKey.subarray(0, 16);

  // Base64 解密密文
  const cipherText = Buffer.from(encrypt, 'base64');

  // AES-256-CBC 解密
  const decipher = createDecipheriv('aes-256-cbc', aesKey, iv);
  decipher.setAutoPadding(false);

  let decrypted: Uint8Array = Buffer.concat([
    decipher.update(cipherText),
    decipher.final()
  ]);

  // 移除 PKCS#7 padding
  decrypted = pkcs7Unpadding(decrypted);

  // 解析格式: 16字节random + 4字节msg_len + msg + corpId
  const msgLenBuf = Buffer.from(decrypted.subarray(16, 20));
  const msgLen = msgLenBuf.readUInt32BE(0);
  const msg = Buffer.from(decrypted.subarray(20, 20 + msgLen)).toString('utf8');

  // ✅ FIX: Trim corpId to remove any trailing null bytes or whitespace that could
  //    cause corpId !== WECOM_CORP_ID even when they are logically equal.
  const corpId = Buffer.from(decrypted.subarray(20 + msgLen))
    .toString('utf8')
    .replace(/\0/g, '')
    .trim();

  return { msg, corpId };
}

// =====================================================
// 辅助函数：从 XML 中提取 Encrypt 字段
// =====================================================
function extractEncryptFromXml(xml: string): string {
  const match = xml.match(/<Encrypt>(<!\[CDATA\[)?(.*?)(\]\]>)?<\/Encrypt>/s);
  return match ? match[2] : '';
}

// =====================================================
// 辅助函数：从解密后的 XML 中提取文本消息内容
// =====================================================
function extractContentFromXml(xml: string): string {
  const match = xml.match(/<Content>(<!\[CDATA\[)?([\s\S]*?)(\]\]>)?<\/Content>/);
  return match ? match[2].trim() : '';
}

function extractMsgTypeFromXml(xml: string): string {
  const match = xml.match(/<MsgType>(<!\[CDATA\[)?(.*?)(\]\]>)?<\/MsgType>/);
  return match ? match[2].trim() : '';
}

function extractFromUserFromXml(xml: string): string {
  const match = xml.match(/<FromUserName>(<!\[CDATA\[)?(.*?)(\]\]>)?<\/FromUserName>/);
  return match ? match[2].trim() : '';
}

// =====================================================
// GET: URL 验证
// 企业微信在配置回调 URL 时发送此请求，需正确响应才能保存配置
// =====================================================
export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const msg_signature = searchParams.get('msg_signature');
  const timestamp = searchParams.get('timestamp');
  const nonce = searchParams.get('nonce');
  const echostr = searchParams.get('echostr');

  console.log('=== WeCom Webhook GET (URL Verification) ===');
  console.log('msg_signature:', msg_signature);
  console.log('timestamp:', timestamp);
  console.log('nonce:', nonce);
  console.log('echostr (first 50):', echostr?.substring(0, 50) + '...');
  console.log('WECOM_TOKEN set:', !!WECOM_TOKEN);
  console.log('WECOM_ENCODING_AES_KEY set:', !!WECOM_ENCODING_AES_KEY, `(${WECOM_ENCODING_AES_KEY.length} chars)`);
  console.log('WECOM_CORP_ID set:', !!WECOM_CORP_ID);

  // 检查环境变量
  if (!WECOM_TOKEN || !WECOM_ENCODING_AES_KEY || !WECOM_CORP_ID) {
    console.error('Missing WeCom environment variables');
    return new NextResponse('Server configuration error', { status: 500 });
  }

  // 检查必需参数
  if (!msg_signature || !timestamp || !nonce || !echostr) {
    console.error('Missing required query parameters');
    return new NextResponse('Missing parameters', { status: 400 });
  }

  try {
    // 调试：打印计算签名
    const computedSignature = sha1Signature(WECOM_TOKEN, timestamp, nonce, echostr);
    console.log('Computed signature:', computedSignature);
    console.log('Received signature:', msg_signature);
    console.log('Signature match:', computedSignature === msg_signature);

    // 1. 验证签名
    if (!verifySignature(WECOM_TOKEN, timestamp, nonce, echostr, msg_signature)) {
      console.error('Signature verification failed');
      return new NextResponse('Signature verification failed', { status: 401 });
    }

    // 2. 解密 echostr（使用修复后的 IV）
    const { msg: decryptedMsg, corpId } = decryptWeComMessage(echostr, WECOM_ENCODING_AES_KEY);

    console.log('Decrypted msg:', decryptedMsg);
    console.log('Decrypted CorpID:', JSON.stringify(corpId));
    console.log('Expected CorpID:', JSON.stringify(WECOM_CORP_ID));
    console.log('CorpID match:', corpId === WECOM_CORP_ID);

    // 3. 验证 CorpID
    if (corpId !== WECOM_CORP_ID) {
      console.error(`CorpID mismatch: got "${corpId}", expected "${WECOM_CORP_ID}"`);
      return new NextResponse('CorpID mismatch', { status: 401 });
    }

    console.log('✅ WeCom URL verification SUCCESS');
    console.log('============================================');

    // 4. 返回纯文本 — 企业微信要求直接返回解密后的 echostr，不带引号或换行
    return new NextResponse(decryptedMsg, {
      status: 200,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' }
    });

  } catch (error) {
    console.error('Error during URL verification:', error);
    return new NextResponse('Decryption failed', { status: 500 });
  }
}

// =====================================================
// POST: 接收企业微信消息
// 解密后处理文本消息，提取 URL 并存入 Notion（Step 1）
// =====================================================
export async function POST(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;
  const msg_signature = searchParams.get('msg_signature');
  const timestamp = searchParams.get('timestamp');
  const nonce = searchParams.get('nonce');

  // 检查环境变量
  if (!WECOM_TOKEN || !WECOM_ENCODING_AES_KEY || !WECOM_CORP_ID) {
    console.error('Missing WeCom environment variables');
    return new NextResponse('Server configuration error', { status: 500 });
  }

  // 检查必需签名参数
  if (!msg_signature || !timestamp || !nonce) {
    return new NextResponse('Missing signature parameters', { status: 400 });
  }

  try {
    const rawBody = await request.text();
    console.log('=== WeCom Webhook POST ===');
    console.log('Raw body (first 300):', rawBody.substring(0, 300));

    // 1. 提取 Encrypt 字段
    const encrypt = extractEncryptFromXml(rawBody);
    if (!encrypt) {
      console.error('No <Encrypt> field found in XML body');
      return new NextResponse('Invalid XML format', { status: 400 });
    }

    // 2. 验证签名
    if (!verifySignature(WECOM_TOKEN, timestamp, nonce, encrypt, msg_signature)) {
      console.error('POST signature verification failed');
      return new NextResponse('Signature verification failed', { status: 401 });
    }

    // 3. 解密消息（使用修复后的 IV）
    const { msg: decryptedXml, corpId } = decryptWeComMessage(encrypt, WECOM_ENCODING_AES_KEY);

    console.log('Decrypted XML:', decryptedXml);
    console.log('CorpID:', corpId);

    // 4. 只处理文本消息（MsgType = text）
    const msgType = extractMsgTypeFromXml(decryptedXml);
    if (msgType !== 'text') {
      console.log(`Non-text message type "${msgType}" — skipping`);
      return new NextResponse('success', {
        status: 200,
        headers: { 'Content-Type': 'text/plain' }
      });
    }

    // 5. 提取文本内容
    const content = extractContentFromXml(decryptedXml);
    const fromUser = extractFromUserFromXml(decryptedXml);
    console.log(`Message from ${fromUser}: ${content}`);

    // 6. TODO (Step 1): 调用 processMessage(content) 提取 URL 并存入 Notion
    //    await processMessage(content);

    // 企业微信要求必须返回字符串 "success"，否则会重试
    return new NextResponse('success', {
      status: 200,
      headers: { 'Content-Type': 'text/plain' }
    });

  } catch (error) {
    console.error('Error processing POST webhook:', error);
    // 注意：即使出错也返回 200 + "success"，否则企业微信会无限重试
    return new NextResponse('success', {
      status: 200,
      headers: { 'Content-Type': 'text/plain' }
    });
  }
}
