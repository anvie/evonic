'use strict';

const express = require('express');
const pino = require('pino');
const QRCode = require('qrcode');
const fs = require('fs');
const path = require('path');
const { attachmentTransportReady, submitAttachment } = require('./attachment-message');
const { downloadInboundDocument, extractInboundText } = require('./inbound-document');
const { extractQuotedMessage, unwrapMessage } = require('./quoted-message');
const { normalizeRecipientJid } = require('./jid');
const { OutboundLifecycle } = require('./outbound-lifecycle');

const PORT = parseInt(process.env.PORT || '3001', 10);
const CALLBACK_URL = process.env.CALLBACK_URL || '';
const CALLBACK_SECRET = process.env.CALLBACK_SECRET || '';
const AUTH_DIR = path.resolve(process.env.AUTH_DIR || './auth_info');
const OWNER_DIR = `${AUTH_DIR}.owner`;

const logger = pino({ level: 'warn' });
const app = express();
app.use(express.json());

// Connection state
let sock = null;
let currentQR = null;
let connectionStatus = 'disconnected'; // 'disconnected' | 'qr_pending' | 'connected'
let isShuttingDown = false;
let botId = '';   // PN-based JID (e.g. 628xxx:1@s.whatsapp.net)
let botLid = '';  // LID-based JID (e.g. 123456:1@lid)
let lastPushedStatus = '';
let saveCredsNow = null;
let pendingCredsSave = Promise.resolve();
let ownerAcquired = false;
let httpServer = null;

// Kept outside startBaileys() so correlation and retry state survive reconnects.
const outboundLifecycle = new OutboundLifecycle({
    send: (jid, content) => {
        if (!sock || connectionStatus !== 'connected' || !messageSendReady) {
            throw new Error('WhatsApp message transport is not ready');
        }
        return sock.sendMessage(jid, content);
    },
    diagnoseFailure: diagnoseReachoutTimelock,
    emit: (payload) => {
        const level = payload.status === 'failed' ? 'error' : 'log';
        console[level](
            '[whatsapp-bridge] OUTBOUND correlationId=%s status=%s jid=%s retry=%d reason=%s',
            payload.correlation_id, payload.status, payload.jid,
            payload.retry_count, payload.reason || '');
        if (CALLBACK_URL) postCallback(payload);
    },
});

// Hook Baileys' internal pino logger so ACK 463 (and other bad-ack errors)
// bypass the EventBuffer entirely.
//
// Baileys 6.x / early 7.x:
//   logger.warn({ attrs: { id, error } }, 'received error in ack')
//
// Baileys 7.x (463-specific branch):
//   logger.warn({ msgId: id, from }, 'error 463: account restricted ...')
//
// Both emit messages.update afterwards, but the pino hook is synchronous
// and fires first — it is our most reliable signal.
{
    const rawWarn = logger.warn.bind(logger);
    logger.warn = (obj, msg) => {
        if (typeof obj === 'object') {
            // Format A: { attrs: { id, error } }  (generic NACK, Baileys 6.x / 7.x else-branch)
            // Format B: { msgId, from }           (463-specific branch, Baileys 7.x)
            const msgId = obj?.attrs?.id || obj?.msgId || '';
            // Format A carries error in attrs.error; Format B carries it in the message text.
            // Fall back to msg text when attrs.error is absent (e.g. "error 463: ...")
            const codeA = obj?.attrs?.error || '';
            const codeB = typeof msg === 'string' && msg.startsWith('error ') ? msg.split(' ')[1].replace(/[^0-9]/g, '') : '';
            const code = codeA || codeB;
            if (msgId && code) {
                console.log('[whatsapp-bridge] logger.warn hook: msgId=%s code=%s msg=%s', msgId, code, msg);
                outboundLifecycle.handleBadAck(msgId, code).catch(
                    (e) => console.error('[whatsapp-bridge] Bad ACK handler error:', e.message));
            }
        }
        return rawWarn(obj, msg);
    };
}

// Message readiness follows Baileys' authenticated connection state. Internal
// init queries such as fetchProps are optional metadata queries; their timeout
// does not mean Signal encryption state is unavailable.
let messageSendReady = false;

function pidIsAlive(pid) {
    if (!Number.isInteger(pid) || pid <= 0) return false;
    try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
}

function acquireOwner() {
    fs.mkdirSync(path.dirname(AUTH_DIR), { recursive: true });
    for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
            fs.mkdirSync(OWNER_DIR);
            fs.writeFileSync(path.join(OWNER_DIR, 'pid'), `${process.pid}\n`, { flag: 'wx' });
            ownerAcquired = true;
            return;
        } catch (e) {
            if (e.code !== 'EEXIST') throw e;
            let ownerPid = NaN;
            try { ownerPid = parseInt(fs.readFileSync(path.join(OWNER_DIR, 'pid'), 'utf8'), 10); } catch (_) {}
            if (pidIsAlive(ownerPid)) {
                throw new Error(`auth directory is already owned by bridge PID ${ownerPid}`);
            }
            fs.rmSync(OWNER_DIR, { recursive: true, force: true });
        }
    }
    throw new Error('failed to acquire auth directory ownership');
}

function releaseOwner() {
    if (!ownerAcquired) return;
    try {
        const ownerPid = parseInt(fs.readFileSync(path.join(OWNER_DIR, 'pid'), 'utf8'), 10);
        if (ownerPid === process.pid) fs.rmSync(OWNER_DIR, { recursive: true, force: true });
    } catch (_) {}
    ownerAcquired = false;
}

function queueCredsSave(saveCreds) {
    pendingCredsSave = pendingCredsSave.catch(() => {}).then(saveCreds);
    return pendingCredsSave;
}

async function flushCreds() {
    if (saveCredsNow) await queueCredsSave(saveCredsNow);
    await pendingCredsSave;
}

async function diagnoseReachoutTimelock() {
    if (!sock?.fetchAccountReachoutTimelock) return {};
    try {
        const state = await sock.fetchAccountReachoutTimelock();
        const ends = state?.timeEnforcementEnds;
        const diagnostic = {
            reachout_timelocked: Boolean(state?.isActive),
            reachout_enforcement_type: state?.enforcementType || '',
            reachout_enforcement_ends: ends instanceof Date ? ends.toISOString() : '',
        };
        console.error(
            '[whatsapp-bridge] ACK 463 reachout_timelocked=%s enforcement=%s ends=%s',
            diagnostic.reachout_timelocked, diagnostic.reachout_enforcement_type,
            diagnostic.reachout_enforcement_ends);
        return diagnostic;
    } catch (error) {
        console.error('[whatsapp-bridge] ACK 463 reachout timelock lookup failed: %s',
            error?.message || error);
        return { reachout_timelock_lookup_failed: true };
    }
}

// Reconnect control — a single-socket guard prevents overlapping sockets from
// fighting over one credential set (which WhatsApp punishes with a conflict/401
// that used to wipe the session). Only one restart is ever pending at a time.
let reconnectAttempts = 0;
let restartScheduled = false;
let reconnectTimer = null;
// Set when WhatsApp reports connectionReplaced (440). A 401 arriving right after
// a replace is conflict fallout — NOT a genuine logout — so we must not wipe on it.
let sawReplaced = false;
const BASE_RECONNECT_MS = 3000;
const MAX_RECONNECT_MS = 60000;

// Group/sender context caches (in-memory; repopulate after restart)
const groupMetaCache = new Map(); // groupJid -> { subject, ts }
const GROUP_META_TTL_MS = 60 * 60 * 1000;
const pushNameCache = new Map(); // senderDigits -> pushName

async function getGroupSubject(jid) {
    const cached = groupMetaCache.get(jid);
    if (cached && Date.now() - cached.ts < GROUP_META_TTL_MS) return cached.subject;
    try {
        const meta = await sock.groupMetadata(jid);
        const subject = meta?.subject || null;
        groupMetaCache.set(jid, { subject, ts: Date.now() });
        return subject;
    } catch (e) {
        console.error('[whatsapp-bridge] Failed to fetch group metadata for %s: %s', jid, e.message);
        return cached ? cached.subject : null;
    }
}

function pushStatus() {
    if (!CALLBACK_URL || isShuttingDown) return;
    if (connectionStatus === lastPushedStatus) return;
    lastPushedStatus = connectionStatus;
    postCallback({ event: 'status', status: connectionStatus });
}

// Schedule exactly one reconnect with exponential backoff. The restartScheduled
// guard ensures a burst of 'close' events can never fan out into multiple
// concurrent sockets. startBaileys() tears down the previous socket first.
function scheduleRestart() {
    if (restartScheduled || isShuttingDown) return;
    restartScheduled = true;
    const delay = Math.min(BASE_RECONNECT_MS * 2 ** reconnectAttempts, MAX_RECONNECT_MS);
    reconnectAttempts += 1;
    console.log('[whatsapp-bridge] Reconnecting in %dms (attempt %d)', delay, reconnectAttempts);
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        restartScheduled = false;
        startBaileys().catch((e) => console.error('[whatsapp-bridge] Baileys restart error:', e));
    }, delay);
}

async function startBaileys() {
    const baileys = await import('@whiskeysockets/baileys');
    const {
        default: makeWASocket,
        useMultiFileAuthState,
        DisconnectReason,
        fetchLatestBaileysVersion,
        makeCacheableSignalKeyStore,
        downloadMediaMessage,
        areJidsSameUser,
    } = baileys;
    fs.mkdirSync(AUTH_DIR, { recursive: true });

    // Tear down any prior socket before opening a new one — a lingering socket
    // with its listeners still attached would race this one over the same creds
    // and trigger a WhatsApp conflict.
    if (sock) {
        try { sock.ev.removeAllListeners(); } catch (_) {}
        try { sock.end(undefined); } catch (_) {}
        sock = null;
    }

    const { state } = await useMultiFileAuthState(AUTH_DIR);
    const saveCreds = async () => {
        const target = path.join(AUTH_DIR, 'creds.json');
        const temp = `${target}.${process.pid}.${Date.now()}.tmp`;
        const data = JSON.stringify(state.creds, baileys.BufferJSON.replacer);
        const handle = await fs.promises.open(temp, 'wx', 0o600);
        try {
            await handle.writeFile(data, 'utf8');
            await handle.sync();
        } finally {
            await handle.close();
        }
        try {
            await fs.promises.rename(temp, target);
            const dir = await fs.promises.open(AUTH_DIR, 'r');
            try { await dir.sync(); } finally { await dir.close(); }
        } catch (e) {
            await fs.promises.rm(temp, { force: true });
            throw e;
        }
    };
    saveCredsNow = saveCreds;
    const { version } = await fetchLatestBaileysVersion();

    sock = makeWASocket({
        version,
        auth: {
            creds: state.creds,
            keys: makeCacheableSignalKeyStore(state.keys, logger),
        },
        printQRInTerminal: false,
        logger,
    });

    sock.ev.on('creds.update', () => {
        queueCredsSave(saveCreds).catch((e) => {
            console.error('[whatsapp-bridge] Failed to persist credentials:', e.message);
        });
    });

    sock.ev.on('messages.update', (updates) => {
        outboundLifecycle.onMessageUpdates(updates).catch((e) => {
            console.error('[whatsapp-bridge] outbound message update handling failed:', e.message);
        });
    });
    sock.ev.on('message-receipt.update', (updates) => {
        outboundLifecycle.onReceipts(updates);
    });

    sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
        if (qr) {
            currentQR = qr;
            connectionStatus = 'qr_pending';
            console.log('[whatsapp-bridge] QR generated — waiting for scan');
        }

        if (connection === 'open') {
            currentQR = null;
            connectionStatus = 'connected';
            reconnectAttempts = 0;
            sawReplaced = false;
            botId = sock.user?.id || '';
            // Baileys v7 sometimes omits lid from sock.user — fall back to creds.
            botLid = sock.user?.lid || state.creds?.me?.lid || '';
            console.log('[whatsapp-bridge] Connected to WhatsApp (id=%s, lid=%s)', botId, botLid);

            // A Baileys `open` event completes authentication and makes the
            // socket ready for encrypted sends. Its background init queries
            // fetch account metadata and may time out independently; they do not
            // populate signalRepository and must not block message delivery.
            messageSendReady = !!(botId && botId.includes('@'));
            console.log('[whatsapp-bridge] Message delivery ready (botId=%s)', botId || '(empty)');
            outboundLifecycle.onConnection('connected').catch((e) => {
                console.error('[whatsapp-bridge] pending outbound retry failed:', e.message);
            });
        }

        if (connection === 'close') {
            connectionStatus = 'disconnected';
            messageSendReady = false;
            const statusCode = lastDisconnect?.error?.output?.statusCode;
            const terminal = statusCode === DisconnectReason.loggedOut
                || statusCode === DisconnectReason.badSession
                || statusCode === DisconnectReason.connectionReplaced;
            outboundLifecycle.onConnection('disconnected', { terminal }).catch((e) => {
                console.error('[whatsapp-bridge] outbound disconnect handling failed:', e.message);
            });
            if (isShuttingDown) { pushStatus(); return; }

            const reasonName = Object.keys(DisconnectReason)
                .find((k) => DisconnectReason[k] === statusCode) || 'unknown';
            console.log('[whatsapp-bridge] Connection closed (statusCode=%s reason=%s)',
                statusCode, reasonName);

            const requestRepair = (why) => {
                console.log('[whatsapp-bridge] %s — credentials preserved; reconnecting', why);
                currentQR = null;
                pushStatus();
                scheduleRestart();
            };

            switch (statusCode) {
                case DisconnectReason.loggedOut: // 401
                    // Preserve the linked-device credentials. A 401 can follow a
                    // transient socket conflict; unlinking must remain an explicit
                    // user action through /logout.
                    sawReplaced = false;
                    requestRepair('Logged out');
                    break;

                case DisconnectReason.badSession: // 500 — auth files corrupt
                    requestRepair('Bad session');
                    break;

                case DisconnectReason.connectionReplaced: // 440
                    // Another socket took over this session. Reconnecting would
                    // restart the war and end in a false 401 wipe. Keep creds and
                    // reconnect once after a long backoff instead of hammering.
                    console.log('[whatsapp-bridge] Connection replaced — backing off, creds preserved');
                    sawReplaced = true;
                    reconnectAttempts = Math.max(reconnectAttempts, 3); // ~24s+ delay
                    scheduleRestart();
                    break;

                default:
                    // restartRequired (515), timedOut (408), connectionClosed (428),
                    // unavailableService (503), network flaps — all transient.
                    // Keep creds and reconnect with backoff.
                    scheduleRestart();
            }
            pushStatus();
            return;
        }

        pushStatus();
    });

    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return;
        if (!CALLBACK_URL) return;

        for (const msg of messages) {
            if (msg.key.fromMe) continue;

            const from = msg.key.remoteJid || '';
            const isGroup = from.endsWith('@g.us');

            // In groups, remoteJid is the group; the actual sender is in participant
            const participant = isGroup ? (msg.key.participant || '') : '';
            const jid = from;
            const sender = isGroup
                ? (participant.includes('@') ? participant.split('@')[0] : participant)
                : (from.includes('@') ? from.split('@')[0] : from);
            // Alternate identifier: when the chat is LID-addressed, WhatsApp
            // exposes the phone-number JID in senderPn/participantPn. Shared
            // channels match routes against both digit namespaces. Baileys v7
            // frequently OMITS senderPn on the message key, which breaks
            // routes keyed on the phone number — so when it's missing, resolve
            // the phone JID from the signal LID map (getPNForLID does a USync
            // if needed). Without this, a LID sender only carries its @lid
            // digits and a phone-keyed route silently misses.
            let altJid = (isGroup ? msg.key.participantPn : msg.key.senderPn) || '';
            const lidSource = isGroup ? participant : from;
            if (!altJid && lidSource.endsWith('@lid') && sock?.signalRepository?.lidMapping) {
                try {
                    altJid = (await sock.signalRepository.lidMapping.getPNForLID(lidSource)) || '';
                } catch (e) {
                    console.error('[whatsapp-bridge] getPNForLID failed for %s: %s', lidSource, e.message);
                }
            }
            const altSender = altJid.includes('@') ? altJid.split('@')[0].split(':')[0] : altJid;
            const messageId = msg.key.id || '';
            const rawMessage = msg.message || {};
            const content = unwrapMessage(rawMessage);
            const wrapperTypes = Object.keys(rawMessage).filter((key) =>
                key === 'ephemeralMessage' || key === 'viewOnceMessage'
                || key === 'viewOnceMessageV2' || key === 'documentWithCaptionMessage');
            const payloadKeys = Object.keys(content || {});
            const contentType = payloadKeys[0] || 'unknown';
            const messageTimestamp = Number(msg.messageTimestamp || 0) || null;

            // Remember display names of group members so quoted authors resolve
            if (isGroup && msg.pushName && sender) {
                if (pushNameCache.size > 2000) pushNameCache.clear();
                pushNameCache.set(sender, msg.pushName);
            }

            // Extract text, including document captions wrapped by WhatsApp.
            const text = extractInboundText(content);

            // Extract button reply (approval flow)
            const buttonReply = content?.buttonsResponseMessage;
            if (buttonReply) {
                const buttonId = buttonReply.selectedButtonId || '';
                postCallback({ from: sender, jid, message_id: messageId, button_id: buttonId, text: '' });
                continue;
            }

            // Extract media if present. Baileys performs the decryption before
            // bytes are forwarded to the Python channel.
            let image = null;
            if (content?.imageMessage) {
                try {
                    // downloadMediaMessage unwraps ephemeral/view-once internally
                    const buffer = await downloadMediaMessage(msg, 'buffer', {}, { logger });
                    const mimetype = content.imageMessage.mimetype || 'image/jpeg';
                    image = {
                        base64: buffer.toString('base64'),
                        mimetype,
                    };
                } catch (e) {
                    console.error('[whatsapp-bridge] Failed to download image:', e.message);
                }
            }

            let document = null;
            let documentDownloadFailed = false;
            if (content?.documentMessage) {
                try {
                    document = await downloadInboundDocument({
                        message: msg, content, downloadMediaMessage, logger,
                    });
                } catch (e) {
                    documentDownloadFailed = true;
                    console.error('[whatsapp-bridge] Failed to download document:', e.message);
                }
            }

            // Log every inbound message without logging attachment contents.
            console.log('[whatsapp-bridge] MSG id=%s from=%s jid=%s group=%s text_len=%d image=%s document=%s document_download_failed=%s',
                messageId, sender, jid, isGroup, text.length, !!image, !!document,
                documentDownloadFailed);
            if (isGroup) {
                console.log('[whatsapp-bridge] GROUP MSG keys:', JSON.stringify(Object.keys(msg.message || {})),
                    'unwrapped:', JSON.stringify(Object.keys(content || {})));
                console.log('[whatsapp-bridge] GROUP MSG from:', sender, 'text:', text?.substring(0, 100));
            }

            // Extract reply/quoted context (contextInfo lives on whichever message type is present)
            const contextInfo = content?.extendedTextMessage?.contextInfo
                || content?.imageMessage?.contextInfo
                || content?.videoMessage?.contextInfo
                || content?.documentMessage?.contextInfo
                || content?.audioMessage?.contextInfo;
            let quotedDetails = null;
            let quotedIsBot = false;
            let quotedSender = '';
            let quotedSenderName = '';
            const quoted = contextInfo?.quotedMessage;
            if (quoted) {
                quotedDetails = extractQuotedMessage(quoted);
                const quotedParticipant = contextInfo.participant || '';
                if (quotedParticipant) {
                    quotedIsBot = (botId && areJidsSameUser(quotedParticipant, botId))
                        || (botLid && areJidsSameUser(quotedParticipant, botLid));
                    quotedSender = quotedParticipant.split('@')[0].split(':')[0];
                    quotedSenderName = pushNameCache.get(quotedSender) || '';
                } else {
                    console.log('[whatsapp-bridge] WARNING: quoted message without participant:', JSON.stringify(contextInfo));
                }
            }

            // Check if bot is @mentioned
            const mentionedJids = contextInfo?.mentionedJid || [];
            let botMentioned = mentionedJids.some(
                m => (botId && areJidsSameUser(m, botId))
                    || (botLid && areJidsSameUser(m, botLid))
            );
            // Fallback: check text for @bot_number if contextInfo didn't have mentions.
            // In LID-addressed groups the mention text carries the LID digits, not the phone.
            if (!botMentioned && isGroup && text) {
                const botPhone = botId ? botId.split(':')[0].split('@')[0] : '';
                const botLidDigits = botLid ? botLid.split(':')[0].split('@')[0] : '';
                if ((botPhone && text.includes('@' + botPhone)) ||
                    (botLidDigits && text.includes('@' + botLidDigits))) {
                    botMentioned = true;
                }
            }
            if (isGroup) {
                console.log('[whatsapp-bridge] mentionedJids:', JSON.stringify(mentionedJids), 'botMentioned:', botMentioned, 'quotedIsBot:', quotedIsBot, 'botId:', botId, 'botLid:', botLid);
            }

            const groupName = isGroup ? await getGroupSubject(jid) : null;

            postCallback({
                from: sender, jid, message_id: messageId, text, image, document,
                document_download_failed: documentDownloadFailed,
                message_timestamp: messageTimestamp,
                content_type: contentType,
                wrapper_types: wrapperTypes,
                payload_keys: payloadKeys,
                alt_sender: altSender,
                alt_jid: altJid,
                // quoted_text remains for older channel consumers; quoted_message
                // carries media identity even when the quoted item has no caption.
                quoted_text: quotedDetails?.text || null,
                quoted_message: quotedDetails,
                is_group: isGroup,
                bot_mentioned: botMentioned,
                quoted_is_bot: quotedIsBot,
                quoted_sender: quotedSender,
                quoted_sender_name: quotedSenderName,
                group_name: groupName,
                pushName: msg.pushName || '',
            });
        }
    });
}

function postCallback(payload) {
    const http = require('http');
    const url = new URL(CALLBACK_URL);
    const body = JSON.stringify(payload);
    const req = http.request({
        hostname: url.hostname,
        port: url.port || 80,
        path: url.pathname,
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(body),
            'Authorization': `Bearer ${CALLBACK_SECRET}`,
        },
    }, (res) => {
        res.resume(); // drain
    });
    req.on('error', (e) => console.error('[whatsapp-bridge] Callback error:', e.message));
    req.write(body);
    req.end();
}

// ---- REST API ----

app.get('/status', (req, res) => {
    res.json({ status: connectionStatus });
});

app.get('/qr', async (req, res) => {
    if (connectionStatus === 'connected') {
        return res.json({ status: 'connected' });
    }
    if (!currentQR) {
        return res.json({ status: connectionStatus, qr: null });
    }
    try {
        const png = await QRCode.toDataURL(currentQR);
        res.json({ status: 'qr_pending', qr: png });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

app.post('/send', async (req, res) => {
    const { to, text, correlation_id: requestedCorrelationId, session_id: sessionId } = req.body || {};
    if (!to || !text) return res.status(400).json({ error: 'to and text required' });
    if (!sock || connectionStatus !== 'connected') {
        return res.status(503).json({ error: 'WhatsApp message transport is not ready' });
    }
    if (!messageSendReady) {
        return res.status(503).json({ error: 'WhatsApp message transport is not ready' });
    }
    const jid = normalizeRecipientJid(to);
    const correlationId = requestedCorrelationId || `${Date.now()}-${Math.random().toString(36).substring(2, 8)}`;
    console.log('[whatsapp-bridge] SEND requested correlationId=%s to=%s jid=%s len=%d', correlationId, to, jid, text.length);
    try {
        const result = await outboundLifecycle.accept({
            correlationId,
            jid,
            content: { text },
            metadata: sessionId ? { session_id: sessionId } : {},
        });
        if (result.status === 'failed') {
            return res.status(500).json({
                success: false,
                status: result.status,
                correlation_id: correlationId,
                retry_count: result.retry_count,
            });
        }
        res.json({ success: true, status: result.status, correlation_id: correlationId,
            message_id: result.message_id, retry_count: result.retry_count });
    } catch (e) {
        console.error('[whatsapp-bridge] SEND FAIL correlationId=%s to=%s error=%s', correlationId, jid, e.message);
        res.status(500).json({ error: e.message, correlation_id: correlationId });
    }
});

app.post('/send-buttons', async (req, res) => {
    const { to, text, buttons } = req.body || {};
    if (!to || !text || !buttons) return res.status(400).json({ error: 'to, text, buttons required' });
    if (!sock || connectionStatus !== 'connected') {
        return res.status(503).json({ error: 'Not connected to WhatsApp' });
    }
    try {
        const jid = normalizeRecipientJid(to);
        const waButtons = buttons.slice(0, 3).map((b) => ({
            buttonId: b.id,
            buttonText: { displayText: b.title.slice(0, 20) },
            type: 1,
        }));
        await sock.sendMessage(jid, {
            text,
            buttons: waButtons,
            headerType: 1,
        });
        res.json({ success: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

app.post('/typing', async (req, res) => {
    const { to, state } = req.body || {};
    if (!to) return res.status(400).json({ error: 'to required' });
    if (!sock || connectionStatus !== 'connected') {
        return res.status(503).json({ error: 'Not connected to WhatsApp' });
    }
    const jid = normalizeRecipientJid(to);
    try {
        await sock.sendPresenceUpdate(state === 'paused' ? 'paused' : 'composing', jid);
        res.json({ success: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

app.post('/send-file', async (req, res) => {
    const {
        to, filePath, caption, mimeType,
        correlation_id: requestedCorrelationId, session_id: sessionId,
    } = req.body || {};
    if (!to || !filePath) {
        return res.status(400).json({ error: 'to and filePath required' });
    }
    if (!attachmentTransportReady({
        hasSocket: Boolean(sock), connectionStatus, messageSendReady,
    })) {
        return res.status(503).json({ error: 'WhatsApp message transport is not ready' });
    }
    const jid = normalizeRecipientJid(to);
    const correlationId = requestedCorrelationId
        || `${Date.now()}-${Math.random().toString(36).substring(2, 8)}`;
    console.log(
        '[whatsapp-bridge] SEND FILE requested correlationId=%s to=%s jid=%s fileName=%s',
        correlationId, to, jid, path.basename(filePath));
    try {
        const result = await submitAttachment({
            lifecycle: outboundLifecycle,
            correlationId,
            jid,
            fileBuffer: fs.readFileSync(filePath),
            filePath,
            caption,
            mimeType,
            sessionId,
        });
        res.status(result.httpStatus).json(result.body);
    } catch (e) {
        console.error(
            '[whatsapp-bridge] SEND FILE FAIL correlationId=%s to=%s error=%s',
            correlationId, jid, e.message);
        res.status(500).json({ error: e.message, correlation_id: correlationId });
    }
});

app.post('/logout', async (req, res) => {
    try {
        if (sock) await sock.logout();
        res.json({ success: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// ---- Start ----

async function shutdown(signal) {
    if (isShuttingDown) return;
    isShuttingDown = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    restartScheduled = false;
    console.log('[whatsapp-bridge] Shutting down (%s)', signal);
    try { if (sock) sock.ev.removeAllListeners('connection.update'); } catch (_) {}
    try { if (sock) sock.end(undefined); } catch (_) {}
    try { await flushCreds(); } catch (e) {
        console.error('[whatsapp-bridge] Credential flush failed during shutdown:', e.message);
        process.exitCode = 1;
    }
    if (httpServer) await new Promise((resolve) => httpServer.close(resolve));
    releaseOwner();
    process.exit(process.exitCode || 0);
}

try {
    acquireOwner();
    httpServer = app.listen(PORT, '127.0.0.1', () => {
        console.log(`[whatsapp-bridge] Listening on 127.0.0.1:${PORT}`);
        startBaileys().catch((e) => {
            console.error('[whatsapp-bridge] Baileys start error:', e);
            process.exitCode = 1;
            shutdown('startup-error');
        });
    });
} catch (e) {
    console.error('[whatsapp-bridge] Startup refused:', e.message);
    process.exit(73);
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('exit', releaseOwner);
